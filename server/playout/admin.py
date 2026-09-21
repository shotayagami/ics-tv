# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin

from playout.models import PlayoutEvent


@admin.register(PlayoutEvent)
class PlayoutEventAdmin(admin.ModelAdmin):
    """as-run は監査ログ。基本 read-only (運用画面から取り消し/再実行は別 UI)。"""

    list_display = (
        "id",
        "sync_seq",
        "channel",
        "scheduled_at",
        "action",
        "status",
        "actual_at",
    )
    list_filter = ("channel", "status", "action")
    date_hierarchy = "scheduled_at"
    readonly_fields = (
        "idempotency_key",
        "sync_seq",
        "channel",
        "scheduled_at",
        "action",
        "asset",
        "program",
        "live_source",
        "cm_bundle",
        "youtube_slot",
        "params",
        "status",
        "actual_at",
        "note",
        "created_at",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
