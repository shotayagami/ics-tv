# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin

from analytics.models import ProgramView, ViewerPresence


@admin.register(ViewerPresence)
class ViewerPresenceAdmin(admin.ModelAdmin):
    list_display = ("viewer_id", "channel", "program", "last_seen")
    list_filter = ("channel",)
    readonly_fields = ("created_at",)


@admin.register(ProgramView)
class ProgramViewAdmin(admin.ModelAdmin):
    list_display = ("program", "viewer_id", "created_at")
    list_filter = ("program__channel",)
    readonly_fields = ("created_at",)
