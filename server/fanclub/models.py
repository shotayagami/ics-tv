# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ファンクラブ (無料/有料ティア・枠契約)。Phase A は無料ティア(level0)のみ加入可能。

依存方向: fanclub → scheduling/members は可 (逆は禁止)。scheduling のモデル層に
fanclub への FK は作らない (ゲート判定は scheduling.vod が fanclub.services を呼ぶ形で完結させる)。
Series への紐付けは fanclub 側が link テーブルで持つ (sales.cm_advertiser_link と同じ形)。
有料ティア(Stripe)は Phase B。ティア定義自体は作れるが、実際に加入できるかは
fanclub.services.tier_is_joinable (Stripe Price + creator の Connect オンボーディング完了) が絞る。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.fields import EncryptedTextField
from members.models import Member


class CreatorStatus(models.TextChoices):
    ACTIVE = "active", "有効"
    SUSPENDED = "suspended", "停止中"


class CreatorOnboardingStatus(models.TextChoices):
    PENDING = "pending", "審査待ち"
    APPROVED = "approved", "承認済み"
    REJECTED = "rejected", "却下"


class Creator(models.Model):
    """クリエイター(サークル・配信者)。Member(視聴者)/User(スタッフ)とは別の第4の主体。"""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=10, choices=CreatorStatus.choices, default=CreatorStatus.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # 公開ファンクラブページ /fc/<slug>/ の見た目 (#27 §10.2 Must)。画像は URL 参照
    # (SeriesPost.media_url と同じ方式、R2 または外部)。theme_color はページの --accent を
    # 差し替える。**hex 6桁のみ許可** — テンプレートへ素通しするため、バリデータが
    # CSS インジェクションの唯一の防壁になる (services.safe_theme_color も参照)。
    avatar_url = models.CharField(max_length=500, blank=True, default="")
    cover_url = models.CharField(max_length=500, blank=True, default="")
    theme_color = models.CharField(
        max_length=7,
        blank=True,
        default="",
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", "「#38bdf8」形式で指定してください")],
    )
    sns_x_url = models.URLField(max_length=300, blank=True, default="")
    sns_youtube_url = models.URLField(max_length=300, blank=True, default="")
    sns_instagram_url = models.URLField(max_length=300, blank=True, default="")
    website_url = models.URLField(max_length=300, blank=True, default="")

    # 会員限定チャット (#27 §3.1 Should、Fanicon グルチャ型)。NULL=チャット無効 /
    # 0=無料会員以上 / n=有料ティアn以上 (fc_required_level と同じ語彙)。
    chat_required_level = models.PositiveSmallIntegerField(null=True, blank=True, default=0)

    # オンボーディング(#27 Phase B、docs/fanclub.md §3.3/§6.1)。運営 studio 側で審査/本人確認を記録する。
    onboarding_status = models.CharField(
        max_length=8,
        choices=CreatorOnboardingStatus.choices,
        default=CreatorOnboardingStatus.PENDING,
    )
    identity_verified_at = models.DateTimeField(null=True, blank=True)

    # 特定商取引法11条表記 (#27 Phase B、docs/fanclub.md §5.2)。hide_contact_details=True の間は
    # 公開画面で address/phone を表示せず「請求により遅滞なく開示する」方式 (BASE 2022-01 例) に倒す。
    legal_name = models.CharField(max_length=200, blank=True, default="")
    is_individual = models.BooleanField(default=True)
    representative_name = models.CharField(max_length=200, blank=True, default="")
    address = models.CharField(max_length=300, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    hide_contact_details = models.BooleanField(default=True)
    contact_email = models.EmailField(max_length=254, blank=True, default="")
    invoice_registration_number = models.CharField(max_length=20, blank=True, default="")

    # Stripe Connect (#27 Phase B)。有料ティアの入金先。Checkout はプラットフォームが徴収し
    # destination charge でこの connected account へ手数料差引後の残額を自動送金する
    # (資金の滞留を作らない収納代行相当の構成)。stripe_connect_onboarded は
    # account.updated webhook が charges_enabled/details_submitted から同期する。
    stripe_connect_account_id = models.CharField(max_length=100, blank=True, default="")
    stripe_connect_onboarded = models.BooleanField(default=False)

    # クリエイター個人 YouTube チャンネル宛シミュルキャスト (#27 Part B、docs/fanclub.md §5.5・§6.5)。
    # per-creator OAuth (Google Compliance Audit 必須) を避け、クリエイター自身が YouTube Studio で
    # 取得した永続ストリームキーを手動で貼る方式 (Channel.youtube_stream_key と同じ運用)。
    # SlotContract.youtube_destination=creator_channel かつこのキーが設定済みのときのみ、
    # 該当シリーズの on-air 窓中だけ CF Live Output を有効化する (fanclub.tasks の reconciler)。
    youtube_destination_ingest_url = models.CharField(
        max_length=300, blank=True, default="rtmp://a.rtmp.youtube.com/live2"
    )
    youtube_destination_stream_key = EncryptedTextField(
        blank=True, null=True
    )  # #3 DB at-rest 暗号化

    class Meta:
        db_table = "creator"

    def __str__(self):
        return self.name


class CreatorSeriesLink(models.Model):
    """Series ⇔ Creator (sales.cm_advertiser_link と同じ形。scheduling 側へ依存を逆流させない)。"""

    series = models.OneToOneField(
        "scheduling.Series",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="fanclub_link",
    )
    creator = models.ForeignKey(Creator, on_delete=models.PROTECT, related_name="series_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "creator_series_link"
        indexes = [models.Index(fields=["creator"], name="idx_fc_series_link_creator")]

    def __str__(self):
        return f"series={self.series_id} -> creator={self.creator_id}"


class CreatorTier(models.Model):
    """ティア定義 (level0=無料は必須1行、fanclub.services.ensure_free_tier がアプリ層で保証)。

    有料ティアは定義のみ可。実際に加入できるかは fanclub.services.tier_is_joinable が絞る
    (Stripe Price(stripe_price_id) + creator の Stripe Connect オンボーディング完了が必要)。
    """

    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="tiers")
    level = models.PositiveSmallIntegerField()  # 0=無料
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    price_minor = models.PositiveIntegerField(null=True, blank=True)  # null=無料 (level0はNULL必須)
    # 通貨 (ISO 4217 小文字、Stripe の表記に合わせる)。**金額カラムは minor unit**
    # (JPY は zero-decimal なので 1000 = ¥1,000、USD は 2-decimal なので 1000 = $10.00)。
    # 金額カラムをかつて `*_jpy` の名前にしていたのをこの形へ移した。現状は jpy 固定
    # (会員登録が国内前提、webhook も jpy 以外を fail-closed で拒否) だが、
    # **台帳は「実際に何で決済されたか」を持たなければならない**ため列として持つ
    # (`*_jpy` の名前のままでは、この前提が変わったときにデータ移行が要る)。
    # 会員の居住国から通貨は逆算できない (Stripe の通貨は課金した Price が決めるため)。
    currency = models.CharField(max_length=3, default="jpy")
    is_active = models.BooleanField(default=True)
    # Stripe Price (#27 Phase B)。Stripe ダッシュボードで作成した Price の ID を studio で貼り付ける
    # 運用 (subscriptions.Plan.stripe_price_id と同じ手動連携方式)。level0(無料)は常に空。
    stripe_price_id = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "creator_tier"
        ordering = ["level"]
        constraints = [
            models.UniqueConstraint(
                fields=["creator", "level"], name="uq_creator_tier_creator_level"
            ),
            models.CheckConstraint(
                name="chk_creator_tier_free_level_no_price",
                condition=Q(level=0, price_minor__isnull=True) | Q(level__gt=0),
            ),
        ]

    def __str__(self):
        return f"{self.creator_id}:{self.name} (L{self.level})"


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", "在籍中"
    LEFT = "left", "退会済"


class CreatorMembership(models.Model):
    """会員 ↔ クリエイター(ファンクラブ) の在籍。1会員につき1クリエイターあたり1行。

    退会後の再加入は新規行を作らず既存行を再利用する (joined_at/left_at のみ更新)。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="fanclub_memberships")
    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="memberships")
    tier = models.ForeignKey(CreatorTier, on_delete=models.PROTECT, related_name="memberships")
    status = models.CharField(
        max_length=6, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Stripe (#27 Phase B、有料ティアのみ使用。無料ティアは空のまま)。状態は
    # fanclub.webhook (Stripe Webhook) のみが更新する (subscriptions.MemberSubscription と同じ規律)。
    stripe_customer_id = models.CharField(max_length=100, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=100, blank=True, default="")
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # 直近適用した Stripe event の created (unix秒)。at-least-once 配送の順序ガード用
    # (subscriptions.MemberSubscription.last_event_created と同じ思想)。
    last_event_created = models.BigIntegerField(null=True, blank=True)

    # ギフトサブスク機能 (追加提供側) の名残。無料構成にはギフト機能が無いため、この列に
    # 値を書き込む経路は存在せず、参照もされない。列を落とさないのは、追加提供側からの
    # 移行で値を持つ既存行がありうるため (既存行の始末は運用側で行う)。サブスク加入 (Webhook)
    # と退会 (services.leave) は従来どおり防御的にクリアする。
    gift_expires_at = models.DateTimeField(null=True, blank=True)

    # ダウングレード予約の表示用 (#27 §3.1 Must「アップグレード/ダウングレード」)。実体は Stripe の
    # Subscription Schedule 側にあり、こちらは「いつ・どのティアへ下がる予定か」を画面に出すためだけの
    # 写し。適用は Stripe が行い、customer.subscription.updated で tier が切り替わった時点で
    # Webhook がこの2列をクリアする (アップグレードは即時適用のため予約は作らない)。
    pending_tier = models.ForeignKey(
        CreatorTier, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    pending_tier_effective_at = models.DateTimeField(null=True, blank=True)

    # デジタル会員証の会員番号 (#27 §3.1 Must)。creator 単位の連番で、採番は
    # fanclub.services.ensure_member_no が creator 行のロック下で行う。退会後の再加入は行を
    # 再利用するため番号も引き継がれる (会員証の番号が変わらないことをファンに保証する)。
    # 実装前から在籍している行は未採番 (NULL) のままなので、表示時に遅延採番する。
    member_no = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "creator_membership"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "creator"], name="uq_creator_membership_member_creator"
            ),
            models.UniqueConstraint(
                fields=["creator", "member_no"],
                condition=Q(member_no__isnull=False),
                name="uq_creator_membership_member_no",
            ),
        ]
        indexes = [
            models.Index(fields=["creator", "status"], name="idx_fc_membership_cr_status"),
        ]

    def __str__(self):
        return f"fc m={self.member_id} c={self.creator_id} {self.status}"

    @property
    def member_no_display(self) -> str:
        """会員証に出す会員番号 (5桁ゼロ詰め)。未採番は '—'。"""
        return f"{self.member_no:05d}" if self.member_no else "—"

    @property
    def enrolled_months(self) -> int:
        """加入からの継続月数 (会員証の継続表示用)。joined_at 未設定は 0。

        「日付が来ていない月」は数えない (4/10 加入なら 5/9 までは0ヶ月、5/10 で1ヶ月)。
        退会→再加入は joined_at が新しくなるため継続はリセットされる (「継続」の意味を守る)。
        """
        if self.joined_at is None:
            return 0
        joined = timezone.localtime(self.joined_at)
        now = timezone.localtime()
        months = (now.year - joined.year) * 12 + (now.month - joined.month)
        if now.day < joined.day:
            months -= 1
        return max(months, 0)

    # 勤続バッジの段階 (#27 §3.1 Should、YouTube の継続バッジ型)。降順で最初に達した段。
    LOYALTY_STEPS: tuple[tuple[int, str], ...] = (
        (24, "継続2年"),
        (12, "継続1年"),
        (6, "継続6ヶ月"),
        (3, "継続3ヶ月"),
        (1, "継続1ヶ月"),
    )

    @property
    def loyalty_badge(self) -> str:
        """継続月数に応じた勤続バッジのラベル。1ヶ月未満は '' (バッジなし)。"""
        months = self.enrolled_months
        for threshold, label in self.LOYALTY_STEPS:
            if months >= threshold:
                return label
        return ""


class SlotContractStatus(models.TextChoices):
    DRAFT = "draft", "draft"
    ACTIVE = "active", "active"
    SUSPENDED = "suspended", "suspended"
    ENDED = "ended", "ended"


class SlotDestination(models.TextChoices):
    OPERATOR_CHANNEL = "operator_channel", "運営チャンネル"
    CREATOR_CHANNEL = "creator_channel", "クリエイター自チャンネル"
    NONE = "none", "配信連携なし"


class SlotContract(models.Model):
    """配信枠のサブスク契約台帳(B2B)。

    2026-07-28: Stripe Billing 化(プラットフォーム直接課金・Connect 不使用、契約ごとに
    金額が異なるため price_data の動的 Price を使う)。ステータスは Stripe 連携済み
    (stripe_subscription_id 設定済み)の間は Webhook のみが更新する(CreatorMembership と
    同じ規律。手動編集すると DB とStripe の実状態が食い違う)。未連携(draft のまま/手動運用)の
    契約は引き続き studio で手動編集できる。
    """

    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="slot_contracts")
    title = models.CharField(max_length=200)
    monthly_fee_minor = models.PositiveIntegerField()
    # 通貨 (ISO 4217 小文字、Stripe の表記に合わせる)。**金額カラムは minor unit**
    # (JPY は zero-decimal なので 1000 = ¥1,000、USD は 2-decimal なので 1000 = $10.00)。
    # 金額カラムをかつて `*_jpy` の名前にしていたのをこの形へ移した。現状は jpy 固定
    # (会員登録が国内前提、webhook も jpy 以外を fail-closed で拒否) だが、
    # **台帳は「実際に何で決済されたか」を持たなければならない**ため列として持つ
    # (`*_jpy` の名前のままでは、この前提が変わったときにデータ移行が要る)。
    # 会員の居住国から通貨は逆算できない (Stripe の通貨は課金した Price が決めるため)。
    currency = models.CharField(max_length=3, default="jpy")
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=9, choices=SlotContractStatus.choices, default=SlotContractStatus.DRAFT
    )
    youtube_destination = models.CharField(
        max_length=17, choices=SlotDestination.choices, default=SlotDestination.NONE
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # Stripe Billing (プラットフォーム直接課金。CreatorMembership と同じフィールド構成だが
    # Connect ではないため destination/application_fee は無い)。Webhook のみが更新する。
    stripe_customer_id = models.CharField(max_length=100, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=100, blank=True, default="")
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # 直近適用した Stripe event の created (unix秒)。at-least-once 配送の順序ガード用。
    last_event_created = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "slot_contract"
        indexes = [models.Index(fields=["creator", "status"], name="idx_slot_contract_cr_status")]

    def __str__(self):
        return f"{self.title} ({self.creator_id})"


class CreatorYoutubeOutput(models.Model):
    """クリエイター個人チャンネル宛シミュルキャストの CF Live Output 状態 (#27 Part B)。

    SlotContract.youtube_destination=creator_channel の実体化 (docs/fanclub.md §5.5・§6.5)。
    Cloudflare の Live Output は Channel.cf_live_input_id (channel 単位の共有 Live Input) 上に
    作成する。creator の連動シリーズ (CreatorSeriesLink) は複数チャンネルへまたがりうるため
    (creator, channel) の組で1本。fanclub.tasks.reconcile_creator_youtube_outputs が番組の
    on-air/off-air に合わせて enabled を切替える (作成はしない・削除もしない=Cloudflare 側の
    Output リソース自体は常設、可視/不可視だけを窓制御する)。
    """

    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="youtube_outputs")
    channel = models.ForeignKey("core.Channel", on_delete=models.CASCADE, related_name="+")
    cf_output_uid = models.CharField(max_length=100)
    # reconciler が最後に CF へ送った (と信じている) enabled 状態。周期ごとに desired と比較し
    # 食い違えば再送するため、CF 側との一時的な不整合は次周期で自己修復する。
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "creator_youtube_output"
        constraints = [
            models.UniqueConstraint(
                fields=["creator", "channel"], name="uq_creator_youtube_output"
            ),
        ]

    def __str__(self):
        return f"{self.creator_id}@{self.channel_id} ({self.cf_output_uid})"


class CreatorAccount(models.Model):
    """creator.* ホストへログインするクリエイター本人のアカウント (Google招待サインイン)。"""

    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="accounts")
    email = models.EmailField(max_length=254)
    google_sub = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "creator_account"
        constraints = [
            models.UniqueConstraint(
                Lower("email"), "creator", name="uq_creator_account_creator_email"
            ),
            models.UniqueConstraint(
                fields=["google_sub"],
                condition=Q(google_sub__gt=""),
                name="uq_creator_account_google_sub",
            ),
        ]

    def __str__(self):
        return f"{self.email} ({self.creator_id})"


class CreatorInvitation(models.Model):
    """クリエイター招待 (トークンURL方式)。studio から発行、creator.* の /invite/<token>/ で受諾。"""

    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField(max_length=254)
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "creator_invitation"

    def __str__(self):
        return f"invite {self.email} -> creator={self.creator_id}"


class FcProcessedStripeEvent(models.Model):
    """処理済み Stripe Webhook イベント (FC有料ティア専用、#27 Phase B)。

    subscriptions.ProcessedStripeEvent と同じ冪等化の思想だが、別アプリ(別webhookエンドポイント/
    別secret)のため独立したテーブルを持つ。
    """

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fanclub_processed_stripe_event"

    def __str__(self):
        return f"fc_stripe_event {self.event_id} {self.event_type}"


class FcChatMessage(models.Model):
    """会員限定チャットの発言 (#27 §3.1 Should)。members.Comment (チャンネルコメント) と同型。

    読む/書くの資格は creator.chat_required_level × 在籍ティア (services.can_use_chat)。
    投稿は HTTP (認証+CSRF)、配信は WS ブロードキャスト (#COMM-01 と同じ分離)。
    退会で CASCADE 削除 (PII)。非表示は deleted_at (soft delete)。
    """

    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="chat_messages")
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="+")
    body = models.TextField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        # creator_page がクエリ後に動的付与するテンプレート専用の勤続バッジ表示
        # (SeriesPost.locked と同じパターン)。DB 列ではない。
        badge: str

    class Meta:
        db_table = "fanclub_chat_message"
        indexes = [
            models.Index(fields=["creator", "-created_at"], name="idx_fc_chat_cr_created"),
        ]

    def __str__(self):
        return f"fcchat#{self.pk} c={self.creator_id} m={self.member_id}"


class FcSettlement(models.Model):
    """FC有料ティア決済の分配元帳(#27 Phase B)。invoice.payment_succeeded webhook が記録する。

    実際の送金は Stripe の destination charge が都度実行するため滞留は発生しない。
    本テーブルは「いつ・誰から・いくら集金し・手数料を引いた後いくらクリエイターへ渡ったか」の
    会計・開示用の記録であり、送金の実行そのものはここでは行わない(参照専用の台帳)。
    """

    creator = models.ForeignKey(Creator, on_delete=models.PROTECT, related_name="settlements")
    member = models.ForeignKey(
        Member, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    tier = models.ForeignKey(
        CreatorTier, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    stripe_invoice_id = models.CharField(max_length=100, unique=True)
    stripe_charge_id = models.CharField(max_length=100, blank=True, default="")
    gross_amount_minor = models.PositiveIntegerField()
    application_fee_minor = models.PositiveIntegerField()
    net_amount_minor = models.PositiveIntegerField()
    # 通貨 (ISO 4217 小文字、Stripe の表記に合わせる)。**金額カラムは minor unit**
    # (JPY は zero-decimal なので 1000 = ¥1,000、USD は 2-decimal なので 1000 = $10.00)。
    # 金額カラムをかつて `*_jpy` の名前にしていたのをこの形へ移した。現状は jpy 固定
    # (会員登録が国内前提、webhook も jpy 以外を fail-closed で拒否) だが、
    # **台帳は「実際に何で決済されたか」を持たなければならない**ため列として持つ
    # (`*_jpy` の名前のままでは、この前提が変わったときにデータ移行が要る)。
    # 会員の居住国から通貨は逆算できない (Stripe の通貨は課金した Price が決めるため)。
    currency = models.CharField(max_length=3, default="jpy")
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    # チャージバック(異議申立て、docs/fanclub.md §3.3 Should)。true の間は net_amount_minor が
    # 実際にはクリエイターへ渡っていない可能性がある(Stripe側で保留/取消)ことを示す。
    disputed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fanclub_settlement"
        indexes = [
            models.Index(fields=["creator", "created_at"], name="idx_fc_settlement_cr_created"),
        ]

    def __str__(self):
        return f"settlement {self.stripe_invoice_id} creator={self.creator_id} net={self.net_amount_minor}"
