# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員の Django admin (staff 可視化 / 緊急運用)。管理ホスト (/admin/) で動く。"""

from django.contrib import admin
from django.utils import timezone

from members.models import Comment, Member, MemberEmailCode


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "nickname",
        "gender",
        "verified",
        "two_factor_method",
        "is_active",
        "created_at",
    )
    list_filter = ("gender", "two_factor_method", "is_active")
    search_fields = ("email", "nickname")
    ordering = ("-created_at",)
    readonly_fields = (
        "password",
        "totp_secret",
        "email_verified_at",
        "totp_confirmed_at",
        "created_at",
        "updated_at",
    )

    @admin.display(boolean=True, description="本人確認済")
    def verified(self, obj):
        return obj.is_verified


@admin.register(MemberEmailCode)
class MemberEmailCodeAdmin(admin.ModelAdmin):
    """サポート/監査用 (読取専用)。code_hash は伏せ、平文コードは保存していない。"""

    list_display = ("member", "purpose", "attempts", "consumed_at", "expires_at", "created_at")
    list_filter = ("purpose",)
    search_fields = ("member__email",)
    ordering = ("-created_at",)
    readonly_fields = (
        "member",
        "purpose",
        "code_hash",
        "expires_at",
        "attempts",
        "consumed_at",
        "created_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    """会員コメントのモデレーション (staff)。非表示=deleted_at の soft delete。"""

    list_display = (
        "id",
        "channel",
        "member",
        "short_body",
        "program_title",
        "created_at",
        "hidden",
    )
    list_filter = ("channel", "deleted_at")
    search_fields = ("body", "member__nickname", "member__email")
    ordering = ("-created_at",)
    readonly_fields = ("channel", "member", "body", "program_title", "created_at")
    actions = ("hide_comments", "restore_comments")

    @admin.display(description="本文")
    def short_body(self, obj):
        return (obj.body or "")[:40]

    @admin.display(boolean=True, description="非表示")
    def hidden(self, obj):
        return obj.deleted_at is not None

    @admin.action(description="選択したコメントを非表示にする")
    def hide_comments(self, request, queryset):
        queryset.filter(deleted_at__isnull=True).update(deleted_at=timezone.now())

    @admin.action(description="選択したコメントを再表示する")
    def restore_comments(self, request, queryset):
        queryset.update(deleted_at=None)
