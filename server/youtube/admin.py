# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin

from youtube.models import (
    ProgramBroadcast,
    YoutubeBroadcastPreset,
    YoutubeConfig,
    YoutubeCredential,
    YoutubeSlot,
)


@admin.register(YoutubeSlot)
class YoutubeSlotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "channel",
        "window_start",
        "window_end",
        "status",
        "broadcast_id",
        "next_nudged",
        "ended_nudged",
    )
    list_filter = ("channel", "status", "next_nudged", "ended_nudged")
    date_hierarchy = "window_start"
    readonly_fields = ("broadcast_id",)


@admin.register(YoutubeCredential)
class YoutubeCredentialAdmin(admin.ModelAdmin):
    list_display = ("channel", "token_expiry", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(YoutubeConfig)
class YoutubeConfigAdmin(admin.ModelAdmin):
    list_display = (
        "channel",
        "title_template",
        "privacy",
        "rolling_hours",
        "slot_minutes",
        "nudge_lead_minutes",
    )


@admin.register(YoutubeBroadcastPreset)
class YoutubeBroadcastPresetAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "channel", "privacy", "category_id", "latency")
    list_filter = ("channel", "privacy")
    search_fields = ("name",)


@admin.register(ProgramBroadcast)
class ProgramBroadcastAdmin(admin.ModelAdmin):
    list_display = ("id", "program", "preset", "status", "broadcast_id", "manual", "updated_at")
    list_filter = ("status", "manual")
    readonly_fields = ("broadcast_id", "created_at", "updated_at")
