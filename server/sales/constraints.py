# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""契約駆動 CM 割付の constraint provider (#6 S4/S6/S8/S11)。

settings.ICSTV_FILL_CONSTRAINT_PROVIDER に dotted path で登録し、resolver.fill_break が
candidates/accept/persisted の 3 フックで呼ぶ。resolve 1 実行 = 1 インスタンス (予約本数を保持)。

優先順位: 1 提供(sponsorship) → 2 指定番組(spot_order_program) → 3 線引き(spot_order_band)
  → 4 契約外フリー。
考査ゲート (S8): 優先 1-3 (契約由来) のみ advertiser+cm 両方 approved を要求。フリー(4)は非適用。
業種同一枠: 同一 ad_break に同 industry を 2 本入れない (cm_advertiser_link→advertiser→industry)。
隣接枠 (S4補足): 枠先頭は放送順で直前ブレーク末尾と同 industry を弾く (前後/番組境界の連続防止)。
提供主競合 (S4): 提供のある番組の枠には、提供主と同業種かつ提供主以外の広告主スポットを入れない。
残本数 (S11): spot=target_count−確定airing−未送出placement−予約 / 提供=seconds_per_episode の秒消化。
"""

from __future__ import annotations

from medialib.models import ScreeningStatus
from playout.models import PlayoutEvent, PlayoutStatus
from sales.models import (
    Airing,
    CmAdvertiserLink,
    Placement,
    PlacementMatch,
    Sponsorship,
    SpotOrder,
)
from scheduling.models import AdBreak, AdBreakItem, Program

_UNSENT = (PlayoutStatus.SCHEDULED, PlayoutStatus.EXECUTING)


class ContractConstraintProvider:
    def __init__(self) -> None:
        self._reserved: dict[int, int] = {}  # spot_order_id → この実行の予約本数
        self._base_rem: dict[int, int] = {}  # spot_order_id → 残本数 (予約前)
        self._ind_cache: dict[int, int | None] = {}  # cm_asset_id → industry_id
        self._adv_cache: dict[int, int | None] = {}  # cm_asset_id → advertiser_id
        self._decisions: dict[tuple[int, int], dict] = {}  # (break_id, cm_asset_id) → 割付理由
        self._sponsor_ms: dict[tuple[int, int], int] = {}  # (program_id, sponsorship_id) → 消化ms
        self._sponsor_ctx: dict[
            int, tuple[set, set]
        ] = {}  # program_id → (提供主id集合, 業種id集合)
        self._prec_cache: dict[int, int | None] = {}  # break_id → 直前ブレーク末尾の industry_id

    # ---- 内部ヘルパ ----

    def _industry_of(self, cm) -> int | None:
        aid = cm.asset_id
        if aid not in self._ind_cache:
            self._ind_cache[aid] = (
                CmAdvertiserLink.objects.filter(cm_asset_id=aid)
                .values_list("advertiser__industry_id", flat=True)
                .first()
            )
        return self._ind_cache[aid]

    def _advertiser_of(self, cm) -> int | None:
        aid = cm.asset_id
        if aid not in self._adv_cache:
            self._adv_cache[aid] = (
                CmAdvertiserLink.objects.filter(cm_asset_id=aid)
                .values_list("advertiser_id", flat=True)
                .first()
            )
        return self._adv_cache[aid]

    def _screening_ok(self, cm) -> bool:
        # 契約 CM は広告主紐付け必須 + advertiser/cm 両方 approved (業態考査 + 表現考査)
        link = (
            CmAdvertiserLink.objects.filter(cm_asset_id=cm.asset_id)
            .select_related("advertiser")
            .first()
        )
        if link is None:
            return False
        return (
            cm.screening_status == ScreeningStatus.APPROVED
            and link.advertiser.screening_status == ScreeningStatus.APPROVED
        )

    def _base_remaining(self, so: SpotOrder) -> int:
        if so.id not in self._base_rem:
            airing = Airing.objects.filter(spot_order_id=so.id).count()
            unsent = Placement.objects.filter(
                spot_order_id=so.id,
                ad_break_item__in=PlayoutEvent.objects.filter(
                    ad_break_item__isnull=False, status__in=_UNSENT
                ).values("ad_break_item_id"),
            ).count()
            self._base_rem[so.id] = so.target_count - airing - unsent
        return self._base_rem[so.id]

    def _live_remaining(self, so: SpotOrder) -> int:
        return self._base_remaining(so) - self._reserved.get(so.id, 0)

    def _fillable_materials(self, so: SpotOrder, br) -> list:
        """spot_order の素材のうち枠グリッドに適合 (READY) する CmCreative (seq 順)。"""
        from medialib.models import NormalizeStatus

        return [
            m.cm_asset
            for m in so.materials.select_related("cm_asset__asset").order_by("seq")
            if m.cm_asset.grid == br.grid
            and m.cm_asset.asset.normalize_status == NormalizeStatus.READY
        ]

    def _consumption(self, so: SpotOrder) -> float:
        if so.target_count <= 0:
            return 1.0
        return (so.target_count - self._base_remaining(so)) / so.target_count

    def _sponsorships(self, program, air_date) -> list:
        """当該 program の series に有効な sponsorship (提供)。期間は sponsorship or 契約に従う。"""
        if not program.series_id:
            return []
        rows = []
        for sp in Sponsorship.objects.filter(
            series_id=program.series_id, contract__status="active"
        ).select_related("contract"):
            ps = sp.period_start or sp.contract.period_start
            pe = sp.period_end or sp.contract.period_end
            if ps <= air_date <= pe:
                rows.append(sp)
        return rows

    def _sponsorship_materials(self, sp, br) -> list:
        from medialib.models import NormalizeStatus

        return [
            m.cm_asset
            for m in sp.materials.select_related("cm_asset__asset").order_by("seq")
            if m.cm_asset.grid == br.grid
            and m.cm_asset.asset.normalize_status == NormalizeStatus.READY
        ]

    def _sponsor_remaining_ms(self, program_id, sp) -> int:
        quota = sp.seconds_per_episode * 1000
        return quota - self._sponsor_ms.get((program_id, sp.id), 0)

    def _sponsor_context(self, program, air_date) -> tuple[set, set]:
        """program の提供主 (advertiser_id 集合, industry_id 集合)。提供主競合の判定軸。"""
        if program.id not in self._sponsor_ctx:
            advs, inds = set(), set()
            for sp in self._sponsorships(program, air_date):
                adv = sp.contract.advertiser
                advs.add(adv.id)
                inds.add(adv.industry_id)
            self._sponsor_ctx[program.id] = (advs, inds)
        return self._sponsor_ctx[program.id]

    def program_credit(self, program, air_at) -> dict | None:
        """提供番組なら提供クレジット CG 用の補助情報を返す (docs/casparcg.md §3.5)。

        emit_recorded が番組頭 PLAY_ASSET の params に載せ、agent が CG 1-50 へ派生する。
        S6: scheduling は本 hook 経由で sponsorship に触れる (直 import しない)。"""
        sps = self._sponsorships(program, air_at.date())
        if not sps:
            return None
        # 提供主名を出稿順・重複排除で連結 (「ご覧のスポンサーの提供で…」の差し込み文言)
        names = list(dict.fromkeys(sp.contract.advertiser.name for sp in sps))
        return {"text": "、".join(names), "template": "credit/sponsor"}

    def _preceding_tail_industry(self, br) -> int | None:
        """放送順で直前のブレーク末尾 CM の industry。隣接枠の業種連続を防ぐ判定軸 (S4補足)。

        同一番組内は offset_ms で前のブレーク、番組頭なら直前番組 (同 channel) の最終ブレーク。
        直前ブレークの item は本 resolve で既に永続化済 (fill は air 順) のため DB から引ける。"""
        if br.id not in self._prec_cache:
            self._prec_cache[br.id] = self._compute_preceding_tail(br)
        return self._prec_cache[br.id]

    def _compute_preceding_tail(self, br) -> int | None:
        prog = br.program
        prev = (
            AdBreak.objects.filter(program_id=prog.id, offset_ms__lt=br.offset_ms)
            .order_by("-offset_ms")
            .first()
        )
        if prev is None:  # 番組頭 → 番組境界。直前番組の最終ブレーク
            prev_prog = (
                Program.objects.filter(channel_id=prog.channel_id, start_at__lt=prog.start_at)
                .order_by("-start_at")
                .first()
            )
            if prev_prog is None:
                return None
            prev = AdBreak.objects.filter(program_id=prev_prog.id).order_by("-offset_ms").first()
        if prev is None:
            return None
        last = (
            AdBreakItem.objects.filter(ad_break_id=prev.id)
            .select_related("cm_asset")
            .order_by("-seq")
            .first()
        )
        return self._industry_of(last.cm_asset) if last is not None else None

    # ---- フック ----

    def candidates(self, br, air_at, base_qs):
        program = br.program
        series_id = program.series_id
        channel_id = program.channel_id
        air_date = air_at.date()
        dow = air_at.weekday()  # 0=月
        air_time = air_at.time()

        ordered: list = []
        seen: set[int] = set()

        def _offer(cm, decision):
            if cm.asset_id in seen or not self._screening_ok(cm):
                return
            self._decisions[(br.id, cm.asset_id)] = decision
            ordered.append(cm)
            seen.add(cm.asset_id)

        # 優先1: 提供 (sponsorship)。提供秒数の残がある間だけ最優先で割付
        if series_id:
            for sp in self._sponsorships(program, air_date):
                if self._sponsor_remaining_ms(program.id, sp) <= 0:
                    continue
                for cm in self._sponsorship_materials(sp, br):
                    _offer(
                        cm,
                        {
                            "match_kind": PlacementMatch.SPONSORSHIP,
                            "sponsorship_id": sp.id,
                            "program_id": program.id,
                        },
                    )

        base_orders = (
            SpotOrder.objects.filter(
                channel_id=channel_id,
                period_start__lte=air_date,
                period_end__gte=air_date,
                contract__status="active",
            )
            .select_related("contract")
            .prefetch_related("programs", "bands", "materials__cm_asset__asset")
        )

        # 優先2: 指定番組スポット (spot_order_program が当該 series)
        if series_id:
            prog_orders = [
                so
                for so in base_orders
                if any(p.series_id == series_id for p in so.programs.all())
                and self._live_remaining(so) > 0
            ]
            for so in prog_orders:
                for cm in self._fillable_materials(so, br):
                    _offer(cm, {"match_kind": PlacementMatch.PROGRAM, "spot_order_id": so.id})

        # 優先3: 線引きスポット (band 合致)。消化率昇順で平準化
        band_orders = [
            so
            for so in base_orders
            if self._live_remaining(so) > 0
            and any(
                (b.dow_mask & (1 << dow)) and b.start_time <= air_time < b.end_time
                for b in so.bands.all()
            )
        ]
        band_orders.sort(key=self._consumption)
        for so in band_orders:
            for cm in self._fillable_materials(so, br):
                _offer(cm, {"match_kind": PlacementMatch.BAND, "spot_order_id": so.id})

        # 優先4: 契約外フリー (考査ゲート非適用)。長尺優先→aired_count 昇順 (均等ローテ)
        free = sorted(base_qs, key=lambda c: (-(c.asset.duration_ms or 0), c.aired_count))
        for cm in free:
            if cm.asset_id not in seen:
                ordered.append(cm)
                seen.add(cm.asset_id)
        # accept の提供主競合判定用に提供 context をこの air_date で確定キャッシュ
        self._sponsor_context(program, air_date)
        return ordered

    def accept(self, br, chosen, cm) -> bool:
        # 業種同一枠: 既選択と同 industry を弾く
        ind = self._industry_of(cm)
        if ind is not None and any(self._industry_of(c) == ind for c in chosen):
            return False
        # 隣接枠: 枠先頭は直前ブレーク末尾と同 industry を弾く (S4補足。前方境界は次枠充填時に判定)
        if ind is not None and not chosen and self._preceding_tail_industry(br) == ind:
            return False
        dec = self._decisions.get((br.id, cm.asset_id))
        # 提供主競合: 提供のある番組に、提供主と同業種かつ提供主以外のスポットを入れない
        if dec is None or dec.get("match_kind") != PlacementMatch.SPONSORSHIP:
            sponsor_advs, sponsor_inds = self._sponsor_ctx.get(br.program_id, (set(), set()))
            if sponsor_inds and ind in sponsor_inds:
                adv = self._advertiser_of(cm)
                if adv not in sponsor_advs:  # 提供主自身の追加スポットは許容
                    return False
        if dec is None:
            return True  # フリー素材
        if dec["match_kind"] == PlacementMatch.SPONSORSHIP:
            # 提供秒数 quota 内か (この実行の消化を加味)
            key = (dec["program_id"], dec["sponsorship_id"])
            dur = cm.asset.duration_ms or 0
            sp = Sponsorship.objects.get(pk=dec["sponsorship_id"])
            if self._sponsor_remaining_ms(dec["program_id"], sp) < dur:
                return False
            self._sponsor_ms[key] = self._sponsor_ms.get(key, 0) + dur
            return True
        # spot: 残本数チェック + 予約
        so_id = dec["spot_order_id"]
        so = SpotOrder.objects.get(pk=so_id)
        if self._live_remaining(so) <= 0:
            return False
        self._reserved[so_id] = self._reserved.get(so_id, 0) + 1
        return True

    def persisted(self, br, items) -> None:
        rows = []
        for item in items:
            dec = self._decisions.get((br.id, item.cm_asset_id))
            if dec:
                rows.append(
                    Placement(
                        ad_break_item=item,
                        spot_order_id=dec.get("spot_order_id"),
                        sponsorship_id=dec.get("sponsorship_id"),
                        match_kind=dec["match_kind"],
                    )
                )
        if rows:
            Placement.objects.bulk_create(rows)
