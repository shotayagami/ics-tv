# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ファンクラブの Django admin。Creator 新規作成時は無料ティア(level0)を自動整備する。"""

from django.contrib import admin

from fanclub.models import (
    Creator,
    CreatorAccount,
    CreatorInvitation,
    CreatorMembership,
    CreatorSeriesLink,
    CreatorTier,
    FcChatMessage,
    FcPendingInvoice,
    FcSettlement,
    SlotContract,
)
from fanclub.services import ensure_free_tier


@admin.register(Creator)
class CreatorAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "status",
        "onboarding_status",
        "stripe_connect_onboarded",
        "created_at",
    )
    list_filter = ("status", "onboarding_status", "stripe_connect_onboarded")
    search_fields = ("name", "slug", "stripe_connect_account_id")
    prepopulated_fields = {"slug": ("name",)}
    # Stripe Connect の状態は account.updated webhook 専管 (fanclub.webhook)。手編集させない。
    readonly_fields = ("stripe_connect_account_id", "stripe_connect_onboarded")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        ensure_free_tier(obj)


@admin.register(CreatorSeriesLink)
class CreatorSeriesLinkAdmin(admin.ModelAdmin):
    list_display = ("series", "creator", "created_at")
    search_fields = ("creator__name", "creator__slug")


@admin.register(CreatorTier)
class CreatorTierAdmin(admin.ModelAdmin):
    list_display = ("creator", "level", "name", "price_minor", "stripe_price_id", "is_active")
    list_filter = ("is_active",)
    list_editable = ("is_active",)
    search_fields = ("creator__name", "name", "stripe_price_id")


@admin.register(CreatorMembership)
class CreatorMembershipAdmin(admin.ModelAdmin):
    list_display = ("member", "creator", "tier", "status", "joined_at", "left_at")
    list_filter = ("status",)
    search_fields = (
        "member__email",
        "creator__name",
        "stripe_customer_id",
        "stripe_subscription_id",
    )
    # Stripe 由来の状態は fanclub.webhook 専管 (subscriptions.MemberSubscriptionAdmin と同じ規律)。
    # status/tier/joined_at/left_at は無料ティアのサポート対応で staff が編集する余地を残す。
    readonly_fields = (
        "created_at",
        "stripe_customer_id",
        "stripe_subscription_id",
        "current_period_end",
        "cancel_at_period_end",
        "last_event_created",
    )


@admin.register(SlotContract)
class SlotContractAdmin(admin.ModelAdmin):
    list_display = ("title", "creator", "monthly_fee_minor", "status", "starts_on", "ends_on")
    list_filter = ("status", "youtube_destination")
    search_fields = ("title", "creator__name")


@admin.register(CreatorAccount)
class CreatorAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "creator", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("email", "creator__name")


@admin.register(CreatorInvitation)
class CreatorInvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "creator", "expires_at", "accepted_at", "created_at")
    search_fields = ("email", "creator__name", "token")
    readonly_fields = ("token",)


@admin.register(FcSettlement)
class FcSettlementAdmin(admin.ModelAdmin):
    list_display = (
        "creator",
        "member",
        "gross_amount_minor",
        "application_fee_minor",
        "net_amount_minor",
        "disputed",
        "created_at",
    )
    list_filter = ("disputed",)
    search_fields = ("creator__name", "member__email", "stripe_invoice_id", "stripe_charge_id")
    ordering = ("-created_at",)
    readonly_fields = tuple(f.name for f in FcSettlement._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(FcChatMessage)
class FcChatMessageAdmin(admin.ModelAdmin):
    """会員限定チャットの発言 (#27 §3.1 Should)。運営モデレーション用に deleted_at のみ編集可。"""

    list_display = ("creator", "member", "body", "created_at", "deleted_at")
    list_filter = ("creator",)
    search_fields = ("creator__name", "member__email", "body")
    ordering = ("-created_at",)
    readonly_fields = ("creator", "member", "body", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(FcPendingInvoice)
class FcPendingInvoiceAdmin(admin.ModelAdmin):
    """在籍に紐付けられず保留中の invoice。在籍が確定すると精算行へ変換されて消える。"""

    list_display = (
        "stripe_invoice_id",
        "stripe_subscription_id",
        "stripe_customer_id",
        "created_at",
    )
    search_fields = ("stripe_invoice_id", "stripe_subscription_id", "stripe_customer_id")
    ordering = ("-created_at",)
    readonly_fields = tuple(f.name for f in FcPendingInvoice._meta.fields)

    def has_add_permission(self, request):
        return False
