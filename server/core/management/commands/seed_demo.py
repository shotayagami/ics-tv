# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""デモデータ投入 (フロント動作検証用)。

runserver + `python manage.py seed_demo` で、運行/編成/公開番組表/営業(割付)/請求/納品の各 UI が
実データで埋まる。手動クリック確認とスクリーンショットの土台。冪等 (既存はスキップ)、--reset で
デモ一式 (demo1 channel + DEMO 印 + demo user) を削除して作り直す。

開発専用。本番投入は想定しない (DEBUG 環境での検証/デモ用)。
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

SLUG = "demo1"
MARK = "DEMO"  # 削除対象を識別するプレフィクス


class Command(BaseCommand):
    help = "フロント動作検証用のデモデータを投入する (冪等、--reset で再生成)"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reset", action="store_true", help="既存のデモ一式を削除してから投入")
        parser.add_argument(
            "--force", action="store_true", help="DEBUG=False でも実行する (危険・非推奨)"
        )

    def handle(self, *args, **opts) -> None:
        # 開発専用 (#sec L-12)。本番で誤実行すると既知パスワードの superuser (demo/demo12345) が
        # 常設バックドアとして残る。DEBUG=False では既定で拒否し、--force で明示的にのみ許す。
        if not settings.DEBUG and not opts["force"]:
            raise CommandError(
                "seed_demo は開発 (DEBUG=True) 専用です。既知パスワードの superuser を本番に作らない"
                "よう保護しています。どうしても実行する場合は --force を明示してください。"
            )
        if opts["reset"]:
            self._reset()
        with transaction.atomic():
            self._seed()

    # ---- reset ----

    def _reset(self) -> None:
        from billing.models import BillingPeriod
        from core.models import Channel
        from sales.models import AdContract, Advertiser, Agency, Industry

        Channel.objects.filter(slug=SLUG).delete()  # programs/events/ad_break は cascade
        AdContract.objects.filter(title__startswith=MARK).delete()
        Advertiser.objects.filter(name__startswith=MARK).delete()
        Agency.objects.filter(name__startswith=MARK).delete()
        Industry.objects.filter(code__startswith="demo").delete()
        BillingPeriod.objects.filter(year=2099).delete()
        get_user_model().objects.filter(username="demo").delete()
        self.stdout.write("reset: デモ一式を削除")

    # ---- seed ----

    def _seed(self) -> None:
        self._demo_user()
        channel = self._channel()
        self._agent_status(channel)
        prog_asset, cms = self._media()
        advertiser, _so = self._sales(channel, cms)
        self._schedule(channel, prog_asset)  # program + ad_break → resolve で events/placement
        self._billing(advertiser)
        self.stdout.write(self.style.SUCCESS("seed_demo 完了"))
        self.stdout.write(
            "  ログイン: demo / demo12345 (superuser)\n"
            f"  運行   : /ops/ch/{SLUG}/dashboard/\n"
            f"  編成   : /scheduling/ch/{SLUG}/timeline/\n"
            f"  公開   : /public/ch/{SLUG}/\n"
            "  営業   : /sales/allocation/   請求: /billing/"
        )

    def _demo_user(self):
        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username="demo", defaults={"is_staff": True, "is_superuser": True}
        )
        if created:
            user.set_password("demo12345")
            user.save()
        return user

    def _channel(self):
        from core.models import Channel

        channel, _ = Channel.objects.get_or_create(
            slug=SLUG,
            defaults={"name": "DEMO 1ch", "enabled": True, "agent_token": "demo-token-xyz"},
        )
        return channel

    def _agent_status(self, channel) -> None:
        from playout.models import AgentStatus

        AgentStatus.objects.update_or_create(
            channel=channel,
            defaults={
                "last_heartbeat_at": timezone.now(),
                "queue_depth": 3,
                "caspar_health": "ok",
                "feed_state": "idle",
                "slate_active": False,
            },
        )

    def _media(self):
        from medialib.models import (
            Asset,
            AssetKind,
            CmCreative,
            CmGrid,
            NormalizeStatus,
            ScreeningStatus,
        )

        prog_asset, _ = Asset.objects.get_or_create(
            r2_key="mezzanine/program/demo-ep.mp4",
            defaults={
                "kind": AssetKind.PROGRAM,
                "title": f"{MARK} 本編 30分",
                "duration_ms": 1_800_000,
                "normalize_status": NormalizeStatus.READY,
            },
        )
        cms = []
        for i in range(1, 4):
            a, _ = Asset.objects.get_or_create(
                r2_key=f"mezzanine/cm/demo-{i}.mp4",
                defaults={
                    "kind": AssetKind.CM,
                    "title": f"{MARK} CM{i}",
                    "duration_ms": 15000,
                    "normalize_status": NormalizeStatus.READY,
                },
            )
            cm, _ = CmCreative.objects.get_or_create(
                asset=a,
                defaults={
                    "advertiser": f"{MARK} 広告主{i}",
                    "grid": CmGrid.G15,
                    "screening_status": ScreeningStatus.APPROVED,
                },
            )
            cms.append(cm)
        return prog_asset, cms

    def _sales(self, channel, cms):
        from sales.models import (
            AdContract,
            Advertiser,
            Agency,
            CmAdvertiserLink,
            ContractKind,
            ContractStatus,
            Industry,
            SpotOrder,
            SpotOrderBand,
            SpotOrderMaterial,
        )

        ind, _ = Industry.objects.get_or_create(code="demo-auto", defaults={"name": "自動車"})
        agency, _ = Agency.objects.get_or_create(
            name=f"{MARK} 代理店", defaults={"commission_rate": 15}
        )
        advertiser, _ = Advertiser.objects.get_or_create(
            name=f"{MARK} 自動車販売",
            defaults={"industry": ind, "screening_status": "approved"},
        )
        for cm in cms:
            CmAdvertiserLink.objects.get_or_create(cm_asset=cm, defaults={"advertiser": advertiser})
        contract, _ = AdContract.objects.get_or_create(
            title=f"{MARK} 夏季スポット",
            defaults={
                "kind": ContractKind.SPOT,
                "advertiser": advertiser,
                "agency": agency,
                "period_start": date(2020, 1, 1),
                "period_end": date(2999, 1, 1),
                "status": ContractStatus.ACTIVE,
            },
        )
        so, created = SpotOrder.objects.get_or_create(
            contract=contract,
            channel=channel,
            defaults={
                "period_start": date(2020, 1, 1),
                "period_end": date(2999, 1, 1),
                "target_count": 200,
                "unit_seconds": 15,
                "unit_price": 30000,
            },
        )
        if created:
            SpotOrderBand.objects.create(
                spot_order=so, dow_mask=127, start_time="00:00", end_time="23:59"
            )
            for i, cm in enumerate(cms):
                SpotOrderMaterial.objects.create(spot_order=so, seq=i, cm_asset=cm)
        return advertiser, so

    def _schedule(self, channel, prog_asset) -> None:
        from scheduling.models import AdBreak, CmGrid, Program, ProgramType, Series
        from scheduling.resolver import resolve

        if Program.objects.filter(channel=channel, title__startswith=MARK).exists():
            return  # 既に編成済 (EXCLUDE 衝突回避)
        series, _ = Series.objects.get_or_create(channel=channel, title=f"{MARK} レギュラー番組")
        now = timezone.now()
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        prog = Program.objects.create(
            channel=channel,
            series=series,
            type=ProgramType.RECORDED,
            title=f"{MARK} 本編 30分",
            start_at=start,
            end_at=start + timedelta(milliseconds=1_800_000 + 60000),
            asset=prog_asset,
        )
        AdBreak.objects.create(program=prog, offset_ms=900_000, grid=CmGrid.G15, duration_ms=60000)
        # resolve で playout_event + ad_break_item + placement を生成 (運行/割付ビューが埋まる)
        resolve(channel, now, now + timedelta(hours=3))

    def _billing(self, advertiser) -> None:
        from billing.models import BillingPeriod, Invoice, InvoiceLine, InvoiceStatus
        from sales.models import AdContract

        period, _ = BillingPeriod.objects.get_or_create(year=2099, month=1)
        contract = AdContract.objects.filter(advertiser=advertiser).first()
        if contract and not Invoice.objects.filter(period=period).exists():
            inv = Invoice.objects.create(
                invoice_number="DEMO-2099-01-0001",
                period=period,
                contract=contract,
                bill_to_agency=True,
                subtotal=600000,
                commission_amount=90000,
                tax_amount=51000,
                total=561000,
                status=InvoiceStatus.DRAFT,
            )
            InvoiceLine.objects.create(
                invoice=inv,
                description="スポットCM 当月分 20本",
                quantity=20,
                unit_price=30000,
                amount=600000,
            )
