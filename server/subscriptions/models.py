# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴者向け有料サブスク (Stripe)。会員管理の上に乗る課金エンジン。

既存 billing app は広告収入(AR)用なので別系統。Plan=ティア(松竹梅)が4つの特典 boolean を持ち、
会員の有効サブスク→Plan→特典集合をエンタイトルメントとして解決する (subscriptions.services)。
MemberSubscription の状態は Stripe Webhook が唯一の更新者 (画面からは触らない)。
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from members.models import Member

# エンタイトルメント識別子 (Plan.features / services.entitlements が返すキー)
ENT_AD_FREE = "ad_free"
ENT_HD = "hd"
ENT_COMMENT_PERK = "comment_perk"
ENT_EXCLUSIVE = "exclusive"

# 現在システムが実際に提供している (=宣伝してよい) 特典。未提供のものは課金ページで
# 「準備中」と明示し、景表法/特商法リスク (動かない特典を宣伝する) を避ける。
# exclusive は VOD-01 の subscribers 公開 (会員限定見逃し) で実在化済み。
# 拡大予定: hd=HLS 署名ゲート / ad_free=VOD への CM 挿入。
ENFORCED_ENTITLEMENTS = {ENT_COMMENT_PERK, ENT_EXCLUSIVE}


class PlanInterval(models.TextChoices):
    MONTH = "month", "月額"
    YEAR = "year", "年額"


class Plan(models.Model):
    """サブスクのティア (松竹梅)。Stripe の Price に紐付ける。staff が admin で管理。"""

    name = models.CharField(max_length=50)  # 松 / 竹 / 梅 等
    slug = models.SlugField(max_length=50, unique=True)
    stripe_price_id = models.CharField(max_length=100, blank=True, default="")
    stripe_product_id = models.CharField(max_length=100, blank=True, default="")
    amount = models.PositiveIntegerField(default=0)  # 表示用 (JPY)
    interval = models.CharField(
        max_length=5, choices=PlanInterval.choices, default=PlanInterval.MONTH
    )
    rank = models.PositiveSmallIntegerField(default=0)  # 上位ほど大 (表示順/比較用)
    is_active = models.BooleanField(default=True)
    # 特典 (エンタイトルメント)
    feat_ad_free = models.BooleanField("広告非表示", default=False)
    feat_hd = models.BooleanField("高画質", default=False)
    feat_comment_perk = models.BooleanField("コメント特典", default=False)
    feat_exclusive = models.BooleanField("会員限定コンテンツ", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription_plan"
        ordering = ["rank"]

    def __str__(self):
        return f"{self.name} (¥{self.amount}/{self.get_interval_display()})"

    @property
    def features(self) -> set[str]:
        feats = set()
        if self.feat_ad_free:
            feats.add(ENT_AD_FREE)
        if self.feat_hd:
            feats.add(ENT_HD)
        if self.feat_comment_perk:
            feats.add(ENT_COMMENT_PERK)
        if self.feat_exclusive:
            feats.add(ENT_EXCLUSIVE)
        return feats

    def perk_list(self) -> list[tuple[str, bool]]:
        """課金ページ表示用の (ラベル, 提供中か) リスト。

        プランが ON にしている特典のみ。提供中でない (ENFORCED_ENTITLEMENTS 外) ものは
        「準備中」として見せ、未提供特典を現行の便益として宣伝しないようにする。
        """
        rows = [
            (
                ENT_COMMENT_PERK,
                self.feat_comment_perk,
                "コメント特典（会員バッジ・連投制限の緩和）",
            ),
            (ENT_HD, self.feat_hd, "高画質"),
            (ENT_AD_FREE, self.feat_ad_free, "広告非表示"),
            (ENT_EXCLUSIVE, self.feat_exclusive, "会員限定コンテンツ"),
        ]
        return [(label, key in ENFORCED_ENTITLEMENTS) for key, on, label in rows if on]


class SubStatus(models.TextChoices):
    ACTIVE = "active", "有効"
    TRIALING = "trialing", "トライアル"
    PAST_DUE = "past_due", "支払遅延"
    CANCELED = "canceled", "解約済"
    INCOMPLETE = "incomplete", "未完了"
    UNPAID = "unpaid", "未払い"


class MemberSubscription(models.Model):
    """会員 ↔ Stripe サブスク。1 会員 1 サブスク。状態は Webhook が同期する。"""

    member = models.OneToOneField(Member, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(
        Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    stripe_customer_id = models.CharField(max_length=100, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=SubStatus.choices, default=SubStatus.INCOMPLETE
    )
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # 最後に適用した subscription 系イベントの event.created (unix秒)。Stripe は at-least-once かつ
    # 順序保証なしのため、これより古いイベントは stale として捨てる (#sec L-3)。
    last_event_created = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "member_subscription"
        indexes = [models.Index(fields=["stripe_subscription_id"], name="idx_sub_stripe_sub")]

    def __str__(self):
        return f"sub m={self.member_id} {self.status}"

    @property
    def is_active(self) -> bool:
        """有効に課金されている状態 (特典付与の判定軸)。"""
        if self.status not in (SubStatus.ACTIVE, SubStatus.TRIALING):
            return False
        return self.current_period_end is None or self.current_period_end > timezone.now()


class ProcessedStripeEvent(models.Model):
    """処理済み Stripe Webhook イベント (event.id 一意)。冪等化用 (#sec L-3)。

    Stripe は at-least-once 配送のため同一 event が再送されうる。event_id を記録し、既処理なら
    スキップする。記録は handle_event の transaction 内で行い、ハンドラ失敗でロールバックされる
    (= 失敗イベントは未処理のまま Stripe の再送で再試行できる)。
    """

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription_processed_stripe_event"

    def __str__(self):
        return f"stripe_event {self.event_id} {self.event_type}"
