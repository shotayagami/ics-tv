# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サブスクの Django admin。Plan は staff が CRUD、MemberSubscription は読取専用 (Webhook 管理)。"""

from django.contrib import admin

from subscriptions.models import MemberSubscription, Plan


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "amount",
        "interval",
        "rank",
        "is_active",
        "feat_ad_free",
        "feat_hd",
        "feat_comment_perk",
        "feat_exclusive",
    )
    list_editable = ("rank", "is_active")
    list_filter = ("is_active", "interval")
    search_fields = ("name", "slug", "stripe_price_id")


@admin.register(MemberSubscription)
class MemberSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "member",
        "plan",
        "status",
        "current_period_end",
        "cancel_at_period_end",
        "updated_at",
    )
    list_filter = ("status", "plan")
    search_fields = ("member__email", "stripe_customer_id", "stripe_subscription_id")
    ordering = ("-updated_at",)
    readonly_fields = (
        "member",
        "plan",
        "stripe_customer_id",
        "stripe_subscription_id",
        "status",
        "current_period_end",
        "cancel_at_period_end",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False
