# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""営放サブシステム sales app (#6 / docs/sales.md)。広告出稿・契約・割付・放確台帳。

依存方向: sales → scheduling/medialib/playout/core (逆は作らない。S6)。割付制約は settings 登録式
provider で注入し、広告主紐付けは cm_advertiser_link が持つ (medialib へ依存を逆流させない)。
Phase A はスポット契約中心。タイム契約 (sponsorship) はテーブルのみ用意し割付ロジックは Phase C。
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from medialib.models import ScreeningStatus


class ContractKind(models.TextChoices):
    TIME = "time", "タイム(番組提供)"
    SPOT = "spot", "スポット"


class ContractStatus(models.TextChoices):
    DRAFT = "draft", "draft"
    ACTIVE = "active", "active"
    FULFILLED = "fulfilled", "fulfilled"
    CANCELLED = "cancelled", "cancelled"
    EXPIRED = "expired", "expired"


class TimeRank(models.TextChoices):
    A = "A", "A"
    SB = "SB", "特B"
    B = "B", "B"
    C = "C", "C"


class PlacementMatch(models.TextChoices):
    SPONSORSHIP = "sponsorship", "提供"  # 優先順位 1
    PROGRAM = "program", "指定番組"  # 優先順位 2
    BAND = "band", "線引き"  # 優先順位 3


class MakeGoodStatus(models.TextChoices):
    OPEN = "open", "open"
    REPLACED = "replaced", "replaced"
    CREDITED = "credited", "credited"


# ---- マスタ ----


class Industry(models.Model):
    """業種マスタ (競合排除の判定軸)。"""

    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=200)

    class Meta:
        db_table = "industry"

    def __str__(self):
        return self.name


class Advertiser(models.Model):
    """広告主マスタ。screening_status = 業態考査 (S8)。"""

    name = models.CharField(max_length=200)
    industry = models.ForeignKey(Industry, on_delete=models.PROTECT, related_name="advertisers")
    screening_status = models.CharField(
        max_length=10, choices=ScreeningStatus.choices, default=ScreeningStatus.PENDING
    )
    note = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "advertiser"

    def __str__(self):
        return self.name


class CmAdvertiserLink(models.Model):
    """CM素材 ⇔ 広告主 (S6: sales 側で持ち medialib へ依存を逆流させない)。"""

    cm_asset = models.OneToOneField(
        "medialib.CmCreative",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="advertiser_link",
    )
    advertiser = models.ForeignKey(Advertiser, on_delete=models.PROTECT, related_name="cm_links")

    class Meta:
        db_table = "cm_advertiser_link"
        indexes = [models.Index(fields=["advertiser"], name="idx_cm_adv_link_advertiser")]


class Agency(models.Model):
    """広告代理店マスタ (S2: 契約の agency=NULL で直販)。"""

    name = models.CharField(max_length=200)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=15.00)
    contact_email = models.EmailField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "agency"

    def __str__(self):
        return self.name


class TimeBandRank(models.Model):
    """タイムランク定義 (channel×曜日×時間帯 → ランク)。区間は [start,end) 半開。"""

    channel = models.ForeignKey("core.Channel", on_delete=models.CASCADE, related_name="+")
    dow = models.IntegerField()  # 0=月..6=日
    start_time = models.TimeField()
    end_time = models.TimeField()
    rank = models.CharField(max_length=2, choices=TimeRank.choices)

    class Meta:
        db_table = "time_band_rank"
        constraints = [
            models.CheckConstraint(name="chk_tbr_dow", condition=Q(dow__gte=0) & Q(dow__lte=6)),
            models.CheckConstraint(
                name="chk_tbr_time", condition=Q(start_time__lt=models.F("end_time"))
            ),
        ]


class RateCard(models.Model):
    """料金表 (正価。S9: 本数×単価)。"""

    channel = models.ForeignKey("core.Channel", on_delete=models.CASCADE, related_name="+")
    rank = models.CharField(max_length=2, choices=TimeRank.choices)
    unit_seconds = models.IntegerField()  # 15/20/30
    price = models.IntegerField()  # 円
    effective_from = models.DateField()

    class Meta:
        db_table = "rate_card"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "rank", "unit_seconds", "effective_from"],
                name="uq_rate_card",
            )
        ]


# ---- 契約 ----


class AdContract(models.Model):
    """契約ヘッダ (タイム/スポット共通)。"""

    kind = models.CharField(max_length=8, choices=ContractKind.choices)
    advertiser = models.ForeignKey(Advertiser, on_delete=models.PROTECT, related_name="contracts")
    agency = models.ForeignKey(
        Agency, on_delete=models.PROTECT, null=True, blank=True, related_name="contracts"
    )  # NULL = 直販
    title = models.CharField(max_length=300)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(
        max_length=10, choices=ContractStatus.choices, default=ContractStatus.DRAFT
    )
    note = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ad_contract"
        constraints = [
            models.CheckConstraint(
                name="chk_contract_period",
                condition=Q(period_end__gte=models.F("period_start")),
            )
        ]

    def __str__(self):
        return f"[{self.kind}] {self.title}"


class Sponsorship(models.Model):
    """タイム契約明細: 番組提供 (S1)。割付ロジックは Phase C。"""

    contract = models.ForeignKey(AdContract, on_delete=models.CASCADE, related_name="sponsorships")
    series = models.ForeignKey("scheduling.Series", on_delete=models.PROTECT, related_name="+")
    period_start = models.DateField(null=True, blank=True)  # NULL = 契約期間に従う
    period_end = models.DateField(null=True, blank=True)
    seconds_per_episode = models.IntegerField()
    monthly_fee = models.IntegerField()  # 月額(円)
    credit_text = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "sponsorship"
        constraints = [
            models.UniqueConstraint(fields=["contract", "series"], name="uq_sponsorship")
        ]


class SponsorshipMaterial(models.Model):
    sponsorship = models.ForeignKey(Sponsorship, on_delete=models.CASCADE, related_name="materials")
    seq = models.IntegerField()
    cm_asset = models.ForeignKey("medialib.CmCreative", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "sponsorship_material"
        constraints = [
            models.UniqueConstraint(fields=["sponsorship", "seq"], name="uq_sponsorship_material")
        ]


class SpotOrder(models.Model):
    """スポット契約明細: 出稿オーダー (線引きの本体)。"""

    contract = models.ForeignKey(AdContract, on_delete=models.CASCADE, related_name="spot_orders")
    channel = models.ForeignKey("core.Channel", on_delete=models.PROTECT, related_name="+")
    period_start = models.DateField()
    period_end = models.DateField()
    target_count = models.IntegerField()  # 契約本数
    unit_seconds = models.IntegerField()  # 15/20/30
    unit_price = models.IntegerField()  # 1本単価(円)。rate_card 算定値のスナップショット
    note = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "spot_order"
        constraints = [
            models.CheckConstraint(
                name="chk_spot_period",
                condition=Q(period_end__gte=models.F("period_start")),
            )
        ]


class SpotOrderBand(models.Model):
    """線引き: 希望時間帯 (複数可)。dow_mask bit0=月..bit6=日。"""

    spot_order = models.ForeignKey(SpotOrder, on_delete=models.CASCADE, related_name="bands")
    dow_mask = models.IntegerField(default=127)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        db_table = "spot_order_band"
        constraints = [
            models.CheckConstraint(
                name="chk_band_dow_mask", condition=Q(dow_mask__gte=1) & Q(dow_mask__lte=127)
            ),
            models.CheckConstraint(
                name="chk_band_time", condition=Q(start_time__lt=models.F("end_time"))
            ),
        ]


class SpotOrderProgram(models.Model):
    """指定番組 (任意。指定があれば優先割付)。"""

    spot_order = models.ForeignKey(SpotOrder, on_delete=models.CASCADE, related_name="programs")
    series = models.ForeignKey("scheduling.Series", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "spot_order_program"
        constraints = [
            models.UniqueConstraint(fields=["spot_order", "series"], name="uq_spot_order_program")
        ]


class SpotOrderMaterial(models.Model):
    spot_order = models.ForeignKey(SpotOrder, on_delete=models.CASCADE, related_name="materials")
    seq = models.IntegerField()
    cm_asset = models.ForeignKey("medialib.CmCreative", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "spot_order_material"
        constraints = [
            models.UniqueConstraint(fields=["spot_order", "seq"], name="uq_spot_order_material")
        ]


# ---- 割付 ⇔ 契約 / 放確台帳 ----


class Placement(models.Model):
    """割付⇔契約の対応 (S6)。割付理由 match_kind を保存 (割付ビューの帰属表示の正)。"""

    ad_break_item = models.OneToOneField(
        "scheduling.AdBreakItem", on_delete=models.CASCADE, related_name="placement"
    )
    spot_order = models.ForeignKey(
        SpotOrder, on_delete=models.CASCADE, null=True, blank=True, related_name="placements"
    )
    sponsorship = models.ForeignKey(
        Sponsorship, on_delete=models.CASCADE, null=True, blank=True, related_name="placements"
    )
    match_kind = models.CharField(max_length=12, choices=PlacementMatch.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "placement"
        indexes = [
            models.Index(fields=["spot_order"], name="idx_placement_order"),
            models.Index(fields=["sponsorship"], name="idx_placement_sponsorship"),
        ]
        constraints = [
            # ちょうど一方 (spot_order か sponsorship)
            models.CheckConstraint(
                name="chk_placement_one",
                condition=(
                    Q(spot_order__isnull=False, sponsorship__isnull=True)
                    | Q(spot_order__isnull=True, sponsorship__isnull=False)
                ),
            ),
            # sponsorship 帰属 ⇔ sponsorship_id あり
            models.CheckConstraint(
                name="chk_placement_match",
                condition=(
                    Q(match_kind="sponsorship", sponsorship__isnull=False)
                    | (~Q(match_kind="sponsorship") & Q(sponsorship__isnull=True))
                ),
            ),
        ]


class Airing(models.Model):
    """放確台帳 (確定放送実績。1行 = CM 1本の確定オンエア)。program/title/尺はスナップショット。"""

    playout_event = models.ForeignKey(
        "playout.PlayoutEvent", on_delete=models.CASCADE, related_name="airings"
    )
    bundle_seq = models.IntegerField(default=0)  # play_cm=0 / bundle=CmBundleItem.seq (S12)
    channel = models.ForeignKey("core.Channel", on_delete=models.PROTECT, related_name="+")
    cm_asset = models.ForeignKey("medialib.CmCreative", on_delete=models.PROTECT, related_name="+")
    spot_order = models.ForeignKey(
        SpotOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="airings"
    )
    sponsorship = models.ForeignKey(
        Sponsorship, on_delete=models.SET_NULL, null=True, blank=True, related_name="airings"
    )
    program = models.ForeignKey(
        "scheduling.Program", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    program_title = models.TextField(blank=True, null=True)  # スナップショット
    duration_ms = models.BigIntegerField()
    aired_at = models.DateTimeField()  # actual_at (NULL時は scheduled_at 補完)
    aired_at_estimated = models.BooleanField(default=False)
    billed = models.BooleanField(default=False)  # 月次締めでロック
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "airing"
        constraints = [
            models.UniqueConstraint(
                fields=["playout_event", "bundle_seq"], name="uq_airing_event_seq"
            ),
            # spot_order / sponsorship は高々一方 (両 NULL = 契約外)
            models.CheckConstraint(
                name="chk_airing_one",
                condition=~Q(spot_order__isnull=False, sponsorship__isnull=False),
            ),
        ]
        indexes = [
            models.Index(fields=["spot_order", "aired_at"], name="idx_airing_order_period"),
            models.Index(fields=["sponsorship", "aired_at"], name="idx_airing_sponsorship_period"),
            models.Index(
                fields=["aired_at"],
                condition=Q(billed=False),
                name="idx_airing_unbilled",
            ),
        ]


class MakeGood(models.Model):
    """欠送・振替 (make-good)。"""

    spot_order = models.ForeignKey(
        SpotOrder, on_delete=models.CASCADE, null=True, blank=True, related_name="make_goods"
    )
    sponsorship = models.ForeignKey(
        Sponsorship, on_delete=models.CASCADE, null=True, blank=True, related_name="make_goods"
    )
    missed_playout_event = models.ForeignKey(
        "playout.PlayoutEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )  # NULL = 手動起票
    reason = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=10, choices=MakeGoodStatus.choices, default=MakeGoodStatus.OPEN
    )
    replacement_airing = models.ForeignKey(
        Airing, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "make_good"
        constraints = [
            models.CheckConstraint(
                name="chk_mg_one",
                condition=(
                    Q(spot_order__isnull=False, sponsorship__isnull=True)
                    | Q(spot_order__isnull=True, sponsorship__isnull=False)
                ),
            ),
            # 欠送検知の冪等性 (missed_event は高々1 make_good)
            models.UniqueConstraint(
                fields=["missed_playout_event"],
                condition=Q(missed_playout_event__isnull=False),
                name="uq_mg_missed_event",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], condition=Q(status="open"), name="idx_mg_open"),
        ]
