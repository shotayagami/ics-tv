# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""内部連携 API (#22)。社内サブシステム → ICSTV の機械間呼び出し。

天気予報サブシステムの社内管理コンソールが、R2 に置いた
生成クリップを ICSTV の medialib に Asset として登録するために使う。公開フロント/管理 SPA の
session+CSRF とは別系統で、共有トークン (settings.WEATHER_IMPORT_TOKEN, X-Internal-Token
ヘッダ) で認証する。トークン未設定時は常に 401 (= 機能無効)。
"""

from __future__ import annotations

import secrets

from django.conf import settings
from ninja import Router, Schema
from ninja.security import APIKeyHeader

from core.security_log import emit as security_emit
from medialib.models import Asset, AssetKind, NormalizeStatus

router = Router(tags=["internal"])


class _BaseInternalToken(APIKeyHeader):
    """共有トークン (X-Internal-Token) 認証の基底。定数時間比較 + 未設定は fail-closed。

    認証失敗は icstv.security へ token_kind 付きで記録する (#sec §3)。トークン漏洩後の
    オンライン推測やテロップ連打の兆候を、通常 0 件のはずの internal.token fail の急増として
    Loki/Zabbix で検知できるようにする。トークン値そのものはログに載せない。
    """

    param_name = "X-Internal-Token"
    setting_name = ""  # サブクラスで指定
    token_kind = ""

    def authenticate(self, request, key):
        expected = getattr(settings, self.setting_name, "") or ""
        if expected and key and secrets.compare_digest(str(key), expected):
            return key
        security_emit("internal.token", outcome="fail", request=request, token_kind=self.token_kind)
        return None


class InternalToken(_BaseInternalToken):
    setting_name = "WEATHER_IMPORT_TOKEN"
    token_kind = "weather"


class EarthquakeToken(_BaseInternalToken):
    """地震速報サブシステム (別リポ) → 速報発射の共有トークン (settings.EARTHQUAKE_FIRE_TOKEN)。"""

    setting_name = "EARTHQUAKE_FIRE_TOKEN"
    token_kind = "earthquake"


class DeliveryToken(_BaseInternalToken):
    """納品サブシステム (別リポ icstv-delivery) → 完成 asset 登録の共有トークン
    (settings.DELIVERY_REGISTER_TOKEN)。リファクタ Phase 2 seam。未設定なら常に 401。"""

    setting_name = "DELIVERY_REGISTER_TOKEN"
    token_kind = "delivery"


class BackofficeToken(_BaseInternalToken):
    """バックオフィスサブシステム (別リポ icstv-backoffice) → 経理/予算/会員の **読み取り集計**
    共有トークン (settings.BACKOFFICE_READ_TOKEN)。リファクタ Phase 3 read seam。未設定なら常に 401。"""

    setting_name = "BACKOFFICE_READ_TOKEN"
    token_kind = "backoffice"


_KIND = {"program": AssetKind.PROGRAM, "filler": AssetKind.FILLER, "cm": AssetKind.CM}


class WeatherImportIn(Schema):
    r2_key: str
    title: str | None = None
    kind: str | None = "program"


class WeatherImportOut(Schema):
    asset_id: int
    normalize_status: str


@router.post("/internal/weather-import", auth=InternalToken(), response=WeatherImportOut)
def weather_import(request, payload: WeatherImportIn):
    """R2 クリップを medialib Asset 化し正規化を投入する (他番組での流用が目的)。

    source_path=r2://<key> で Asset を作るだけの薄い endpoint。post_save signal が on_commit で
    normalize_asset.delay を投入する。特定の枠への自動バインドとは無関係 (こちらは汎用 medialib 登録)。
    """
    kind = _KIND.get((payload.kind or "program").lower(), AssetKind.PROGRAM)
    key = payload.r2_key.lstrip("/")
    asset = Asset.objects.create(
        kind=kind,
        title=payload.title or f"天気予報素材 {key.split('/')[-1]}",
        source_path=f"r2://{key}",
        normalize_status=NormalizeStatus.PENDING,
    )
    return {"asset_id": asset.id, "normalize_status": asset.normalize_status}


class BreakingTelopIn(Schema):
    text: str | None = None
    op: str | None = "show"  # show | clear
    duration_sec: int | None = None
    channel_slug: str | None = None  # 空=全 enabled ch
    chime: str | None = None  # eew | weather | general (層41で音を併発)。未指定=無音(従来互換)


class BreakingTelopOut(Schema):
    fired: list[str]


@router.post("/internal/breaking-telop", auth=EarthquakeToken(), response=BreakingTelopOut)
def breaking_telop(request, payload: BreakingTelopIn):
    """速報テロップ (layer40) を発射する。地震速報サブシステム (別リポ) と studio 手動送出が叩く。

    op=show は本文を出し duration 秒後に自動クリア、op=clear は即時消去。channel_slug 未指定なら
    全 enabled チャンネルへ。chime(eew|weather|general) を渡すと show 時に layer41 で音を併発する
    (未指定=従来どおり無音)。発射は core.views.fire_breaking_telop (op_overlay と同経路) に委譲する。
    """
    from ninja.errors import HttpError

    from core.models import Channel
    from core.views import fire_breaking_telop

    op = (payload.op or "show").lower()
    text = (payload.text or "").strip()
    if op != "clear" and not text:
        raise HttpError(400, "text 必須")
    qs = Channel.objects.filter(enabled=True)
    if payload.channel_slug:
        qs = qs.filter(slug=payload.channel_slug)
    fired = fire_breaking_telop(
        list(qs), text, duration_sec=payload.duration_sec or 90, op=op, chime=payload.chime
    )
    return {"fired": fired}


class HazardMapIn(Schema):
    kind: str  # tsunami | seismic | seismic_zoom | seismic_regional | eew_panel | eew_band | tsunami_corner
    op: str | None = "show"  # show | update | clear
    data: dict | None = (
        None  # 地図描画データ (予報区別 grade/到達/高さ・観測点 latlon/震度・EEW地域名 等)
    )
    duration_sec: int | None = None
    channel_slug: str | None = None  # 空=全 enabled ch


class HazardMapOut(Schema):
    fired: list[str]


@router.post("/internal/hazard-map", auth=EarthquakeToken(), response=HazardMapOut)
def hazard_map(request, payload: HazardMapIn):
    """地図CG (kind=graphic) を発射する。

    breaking-telop と同じ EarthquakeToken 認証。op=show/update は map/<kind>.html に描画データを
    流し込み (点滅等の演出はテンプレ側 CSS/JS)、続報が途切れて duration 秒で自動 hide。op=clear は
    警報解除で即時消去。地図データ (data) の形は kind により異なる (津波=予報区別、震度=観測点)。
    tsunami/seismic/eew_panel/eew_band は layer38 のフルスクリーン地図 (排他)。tsunami_corner は
    layer37 の常時表示ミニマップ (地図+凡例のみ・画面右下) で、layer38 側と独立に同時 show できる。
    発射は core.views.fire_hazard_map に委譲。地震速報サブシステム (別リポ) と studio 手動発火が叩く。
    """
    from ninja.errors import HttpError

    from core.models import Channel
    from core.views import HAZARD_MAP_KINDS, fire_hazard_map

    kind = (payload.kind or "").strip().lower()
    if kind not in HAZARD_MAP_KINDS:
        raise HttpError(400, "kind は " + " | ".join(sorted(HAZARD_MAP_KINDS)))
    op = (payload.op or "show").lower()
    if op not in ("show", "update", "clear"):
        raise HttpError(400, "op は show | update | clear")
    qs = Channel.objects.filter(enabled=True)
    if payload.channel_slug:
        qs = qs.filter(slug=payload.channel_slug)
    fired = fire_hazard_map(
        list(qs), kind, payload.data, op=op, duration_sec=payload.duration_sec or 120
    )
    return {"fired": fired}


# ---- 納品サブシステム (別リポ icstv-delivery) seam (リファクタ Phase 2) ----


class DeliveryAssetIn(Schema):
    r2_key: str  # DELIVERY 側で正規化済み (mezzanine) の R2 キー
    title: str
    kind: str | None = "program"
    duration_ms: int
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    checksum: str | None = None
    passthrough: bool = False  # 長尺 passthrough (映像 copy + 音声 loudnorm) 素材
    thumbnail_url: str | None = None
    # Episode 特定 (series_episode 納品の本編)。無指定なら asset 登録のみ (backfill しない)。
    series_id: int | None = None
    episode_id: int | None = None
    episode_no: int | None = None
    air_date: str | None = None  # YYYY-MM-DD


class DeliveryAssetOut(Schema):
    asset_id: int
    episode_id: int | None
    backfilled: bool


def _parse_iso_date(s: str | None):
    if not s:
        return None
    from datetime import date

    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# 冪等再送の同一性判定で許容する duration_ms の差。同一原本の再正規化 (crash 後の再実行等) は
# encode 揺れで数 ms ずれ得るが、別素材が同 key に来たら通常は秒単位で違う。
_DELIVERY_RESEND_DURATION_TOLERANCE_MS = 2000


def _is_idempotent_resend(asset: Asset, payload: DeliveryAssetIn, kind: str) -> bool:
    """既存 Asset が「この納品の正当な冪等再送」か (=別納品物との r2_key 衝突でないか)。

    DELIVERY 側の mezzanine key は delivery_file.id を含むため、正当な再送 (seam 応答喪失後の
    再実行等) では kind/title/duration が既存 Asset と一致する。別物なら無警告で旧 Asset に
    差し替わってしまうため、呼び出し側 (delivery_asset) は 409 で reject する (2026-09-02 監査
    決定#3①)。checksum は両方に値があるときのみ比較 (DELIVERY 側は現状送っていない)。
    """
    if payload.checksum and asset.checksum and payload.checksum != asset.checksum:
        return False
    if asset.kind != kind:
        return False
    if asset.title != payload.title:
        return False
    return (
        abs((asset.duration_ms or 0) - payload.duration_ms)
        <= _DELIVERY_RESEND_DURATION_TOLERANCE_MS
    )


def _resolve_delivery_episode(payload: DeliveryAssetIn):
    """納品先 Episode を特定/作成 (delivery.portal._resolve_episode と同規約)。Episode か None。
    series_id も episode_id も無ければ None (= 本編 Episode を持たない登録)。"""
    from ninja.errors import HttpError

    from scheduling.models import Episode

    if payload.episode_id:
        ep = Episode.objects.select_related("series").filter(pk=payload.episode_id).first()
        if ep is None:
            raise HttpError(400, "指定の episode が見つかりません")
        return ep
    if not payload.series_id:
        return None
    air = _parse_iso_date(payload.air_date)
    if payload.episode_no is None and air is None:
        raise HttpError(400, "回の特定に episode_no か air_date が必要です")
    if payload.episode_no is not None:
        ep, _ = Episode.objects.get_or_create(
            series_id=payload.series_id, episode_no=payload.episode_no, defaults={"air_date": air}
        )
        if air and ep.air_date != air:
            ep.air_date = air
            ep.save(update_fields=["air_date"])
    else:
        ep, _ = Episode.objects.get_or_create(series_id=payload.series_id, air_date=air)
    return Episode.objects.select_related("series").get(pk=ep.pk)


@router.post("/internal/delivery-asset", auth=DeliveryToken(), response=DeliveryAssetOut)
def delivery_asset(request, payload: DeliveryAssetIn):
    """ICS-DELIVERY が QC + 正規化まで終えた完成 asset を ICS-TV へ登録する (Phase 2 seam)。

    DELIVERY 側で正規化済みを前提に normalize_status=READY で受ける (本体は再正規化しない)。
    series_episode 本編なら Episode.asset を確定 (status→CONFIRMED) し、apply_episode_asset で
    既存 Program へバックフィルする (delivery.review.confirm_episode_asset + scheduling.signals と
    同じ結線)。冪等: 同一 r2_key かつ同一メタの既存 asset を再利用する。メタが食い違う同一 r2_key
    は別納品物の衝突とみなし 409 で reject する (旧 Asset への無警告差替を防ぐ。決定#3①)。
    """
    import logging

    from django.db import transaction
    from ninja.errors import HttpError

    kind = _KIND.get((payload.kind or "program").lower(), AssetKind.PROGRAM)
    key = payload.r2_key.lstrip("/")

    asset = Asset.objects.filter(r2_key=key).first()  # 冪等
    if asset is not None and not _is_idempotent_resend(asset, payload, kind):
        security_emit(
            "internal.delivery_asset",
            outcome="conflict",
            request=request,
            level=logging.WARNING,
            r2_key=key,
            asset_id=asset.id,
        )
        raise HttpError(409, "同一 r2_key の既存 asset とメタが一致しません (別納品物の衝突)")
    if asset is None:
        asset = Asset.objects.create(
            kind=kind,
            title=payload.title,
            r2_key=key,
            source_path=f"r2://{key}",
            duration_ms=payload.duration_ms,
            width=payload.width,
            height=payload.height,
            fps=payload.fps,
            vcodec=payload.vcodec,
            acodec=payload.acodec,
            checksum=payload.checksum,
            passthrough=payload.passthrough,
            thumbnail_url=payload.thumbnail_url,
            normalize_status=NormalizeStatus.READY,
        )

    ep = _resolve_delivery_episode(payload)
    if ep is None:
        return {"asset_id": asset.id, "episode_id": None, "backfilled": False}

    from scheduling.models import EpisodeStatus

    ep.asset = asset
    if ep.status != EpisodeStatus.AIRED:
        ep.status = EpisodeStatus.CONFIRMED
    ep.save(update_fields=["asset", "status"])

    from scheduling.tasks import apply_episode_asset

    transaction.on_commit(lambda: apply_episode_asset.delay(asset.id))
    return {"asset_id": asset.id, "episode_id": ep.id, "backfilled": True}


class DeliveryRefChannel(Schema):
    id: int
    slug: str
    name: str


class DeliveryRefSeries(Schema):
    id: int
    channel_id: int
    title: str


class DeliveryRefsOut(Schema):
    channels: list[DeliveryRefChannel]
    series: list[DeliveryRefSeries]


@router.get("/internal/delivery-refs", auth=DeliveryToken(), response=DeliveryRefsOut)
def delivery_refs(request):
    """ICS-DELIVERY が納品先 (channel/series) を選ぶための参照データ (読み取り)。
    Episode の get_or_create は登録時 (delivery-asset) に行うため、ここは候補一覧のみ。"""
    from core.models import Channel
    from scheduling.models import Series

    channels = [
        {"id": c.id, "slug": c.slug, "name": c.name}
        for c in Channel.objects.filter(enabled=True).order_by("slug")
    ]
    series = [
        {"id": s.id, "channel_id": s.channel_id, "title": s.title}
        for s in Series.objects.filter(is_active=True).order_by("title")
    ]
    return {"channels": channels, "series": series}


# ---- バックオフィスサブシステム (別リポ icstv-backoffice) read seam (リファクタ Phase 3) ----
#
# read-API-first: backoffice は ICS-TV を真実とし、経理請求/番組予算/会員分析の**集計**を
# GET で取得して表示するだけ (書き所有権の移管は後続フェーズ)。集計ロジックは既存 service を
# 再利用し backoffice 側で再実装しない (二重計算の乖離回避)。すべて BackofficeToken 認証・読み取り専用。


def _parse_period(period: str) -> tuple[int, int]:
    from ninja.errors import HttpError

    try:
        y, m = period.split("-")
        year, month = int(y), int(m)
        if not (1 <= month <= 12):
            raise ValueError
    except ValueError as e:
        raise HttpError(400, "period は YYYY-MM 形式") from e
    return year, month


class BackofficeInvoice(Schema):
    id: int
    advertiser: str
    total: int
    paid: bool


class BackofficeBillingOut(Schema):
    period: str
    invoiced_total: int
    paid_total: int
    outstanding_total: int
    invoices: list[BackofficeInvoice]


@router.get("/internal/backoffice/billing", auth=BackofficeToken(), response=BackofficeBillingOut)
def backoffice_billing(request, period: str):
    """期間 (YYYY-MM) の請求/入金サマリ (billing から集計)。void は除外。"""
    from django.db.models import Sum

    from billing.models import BillingPeriod, Invoice, InvoiceStatus, Payment

    year, month = _parse_period(period)
    bp = BillingPeriod.objects.filter(year=year, month=month).first()
    if bp is None:
        return {
            "period": period,
            "invoiced_total": 0,
            "paid_total": 0,
            "outstanding_total": 0,
            "invoices": [],
        }
    invs = list(
        Invoice.objects.filter(period=bp)
        .exclude(status=InvoiceStatus.VOID)
        .select_related("contract__advertiser")
        .order_by("invoice_number")
    )
    invoiced = sum(i.total for i in invs)
    paid = (
        Payment.objects.filter(invoice__period=bp)
        .exclude(invoice__status=InvoiceStatus.VOID)
        .aggregate(s=Sum("paid_amount"))["s"]
        or 0
    )
    invoices = [
        {
            "id": i.id,
            "advertiser": getattr(getattr(i.contract, "advertiser", None), "name", "") or "",
            "total": i.total,
            "paid": i.status == InvoiceStatus.PAID,
        }
        for i in invs
    ]
    return {
        "period": period,
        "invoiced_total": invoiced,
        "paid_total": paid,
        "outstanding_total": invoiced - paid,
        "invoices": invoices,
    }


class BackofficeBudgetRow(Schema):
    series_id: int
    title: str
    budget: int
    spent: int
    remaining: int


class BackofficeBudgetOut(Schema):
    fiscal_year: int
    rows: list[BackofficeBudgetRow]


@router.get("/internal/backoffice/budget", auth=BackofficeToken(), response=BackofficeBudgetOut)
def backoffice_budget(request, fiscal_year: int):
    """年度の番組予算 vs 実費。

    番組予算 (procurement) は追加提供側の機能で、このツリーには無い (D037)。口と応答の形は
    D036 により残し、常に空の行を返す。追加提供側が接続するときはここの実装を差し替える。
    """
    return {"fiscal_year": fiscal_year, "rows": []}


class BackofficePlanCount(Schema):
    plan: str
    count: int


class BackofficeMemberStatsOut(Schema):
    total: int
    by_plan: list[BackofficePlanCount]
    active_30d: int
    retention_rate: float | None


@router.get(
    "/internal/backoffice/member-stats", auth=BackofficeToken(), response=BackofficeMemberStatsOut
)
def backoffice_member_stats(request):
    """会員数 / プラン別 / 直近 30 日アクティブ の集計 (members + subscriptions)。"""
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from members.models import Member, WatchHistory
    from subscriptions.models import MemberSubscription, SubStatus

    total = Member.objects.count()
    plan_rows = (
        MemberSubscription.objects.filter(status__in=(SubStatus.ACTIVE, SubStatus.TRIALING))
        .values("plan__name")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    by_plan = [
        {"plan": r["plan__name"] or "(プラン未設定)", "count": r["count"]} for r in plan_rows
    ]
    subscribed = sum(r["count"] for r in by_plan)
    by_plan.append({"plan": "無課金", "count": max(0, total - subscribed)})
    cutoff = timezone.now() - timedelta(days=30)
    active_30d = (
        WatchHistory.objects.filter(updated_at__gte=cutoff).values("member").distinct().count()
    )
    return {"total": total, "by_plan": by_plan, "active_30d": active_30d, "retention_rate": None}


class BackofficePickerSeries(Schema):
    id: int
    title: str


class BackofficePickerDelivery(Schema):
    id: int
    title: str
    company_id: int | None
    company_name: str
    series_id: int | None  # delivery_series_id 解決済 (予算ロールアップ用・CM 等は None)


class BackofficePickerOut(Schema):
    series: list[BackofficePickerSeries]
    deliveries: list[BackofficePickerDelivery]


@router.get("/internal/backoffice/picker", auth=BackofficeToken(), response=BackofficePickerOut)
def backoffice_picker(request):
    """番組予算 write フォーム用の選択肢 (Series 一覧 + 納品一覧)。

    `series` は本体の scheduling.Series なのでそのまま返す。納品一覧は番組予算 (procurement) と
    ICS-DELIVERY 読み seam に依存し、どちらも追加提供側の機能でこのツリーには無いため、常に空を
    返す (D036)。追加提供側が接続するときはここの実装を差し替える。
    """
    from scheduling.models import Series

    series = [{"id": s.id, "title": s.title} for s in Series.objects.order_by("title")]
    return {"series": series, "deliveries": []}


class MonitorToken(_BaseInternalToken):
    """外形監視 (Zabbix サーバ、クラスタ外) → agent heartbeat 経過秒の **読み取り専用** 共有トークン
    (settings.MONITOR_READ_TOKEN)。未設定なら常に 401。"""

    setting_name = "MONITOR_READ_TOKEN"
    token_kind = "monitor"


class MonitorHeartbeatChannel(Schema):
    slug: str
    name: str
    # AgentStatus 行が無い (一度も heartbeat が来ていない) channel は None。offline は True。
    seconds_since_heartbeat: float | None
    offline: bool
    on_air: bool


class MonitorHeartbeatOut(Schema):
    now: str  # ISO 8601 (UTC)
    threshold_sec: int
    channels: list[MonitorHeartbeatChannel]


@router.get("/internal/monitor/heartbeat", auth=MonitorToken(), response=MonitorHeartbeatOut)
def monitor_heartbeat(request):
    """有効 channel ごとの agent heartbeat 経過秒。クラスタ外 (Zabbix) からの死活監視の読み出し口。

    死活 beat (playout.tasks.check_agent_liveness) は監視対象クラスタの中の Celery beat/worker で
    動く。beat か worker か Redis が止まれば agent_offline は出ないし、offline_notified が latch
    されたまま誰も見なければそれきり黙る (2026-06-22 退役の ch2 が 74 日間その状態だった)。
    つまり「通知が無い」は「正常」と「検知系ごと死んでいる」を区別できない。

    このエンドポイントは request 経路の icstv-web が agent_status を直接読んで返すだけで Celery を
    経由しない。Zabbix 側は (1) offline で heartbeat 未達を beat とは独立した経路で二重化し、
    (2) 応答自体の途絶 (nodata) を制御プレーン全体の沈黙として拾う。閾値と on_air は
    check_agent_liveness と同じ定義を使い、二重の真実を作らない。

    副作用は無い。DB 行を書かず、通知も出さない (通知は beat 側の責務のまま)。
    """
    from django.utils import timezone

    from core.models import Channel
    from playout.models import AgentStatus
    from playout.tasks import OFFLINE_THRESHOLD_SEC

    now = timezone.now()
    channels = []
    qs = Channel.objects.filter(enabled=True).select_related("agent_status").order_by("slug")
    for ch in qs:
        try:
            st = ch.agent_status
        except AgentStatus.DoesNotExist:
            st = None
        age = (now - st.last_heartbeat_at).total_seconds() if st else None
        channels.append(
            {
                "slug": ch.slug,
                "name": ch.name,
                "seconds_since_heartbeat": round(age, 1) if age is not None else None,
                # 行が無い = 一度も heartbeat が無い。「不明」で隠さず offline として出す。
                "offline": age is None or age > OFFLINE_THRESHOLD_SEC,
                "on_air": ch.is_on_air(now),
            }
        )
    return {"now": now.isoformat(), "threshold_sec": OFFLINE_THRESHOLD_SEC, "channels": channels}
