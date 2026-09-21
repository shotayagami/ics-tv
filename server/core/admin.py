# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin

from core.models import Channel, LiveSource


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = (
        "slug",
        "name",
        "short",
        "tint",
        "enabled",
        "youtube_livestream_id",
        "cf_live_input_id",
    )
    list_filter = ("enabled",)
    search_fields = ("slug", "name")


@admin.register(LiveSource)
class LiveSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "rtmp_app", "rtmp_key", "srt_latency_ms")
    search_fields = ("name",)
    readonly_fields = ("srt_ingest_hint", "rtmp_ingest_hint")

    @admin.display(
        description="SRT ingest URL (現場へ渡す / <host>=WARP 到達の送出ノード private IP)"
    )
    def srt_ingest_hint(self, obj):
        return obj.srt_ingest_url() if obj.pk else "(保存後に表示)"

    @admin.display(description="RTMP ingest URL (LAN/WG 内サブ卓向け)")
    def rtmp_ingest_hint(self, obj):
        return obj.rtmp_ingest_url() if obj.pk else "(保存後に表示)"
