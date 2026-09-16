# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin, messages

from rights.models import DistributionRight
from scheduling.models import (
    AdBreak,
    AdBreakItem,
    Episode,
    Program,
    Series,
    SeriesPost,
)

# 公開面に出してはいけないテスト/仮素材の番組タイトルに含まれがちな語 (#5 テスト露出防止)。
_TEST_TITLE_MARKERS = ("テスト", "test", "サンプル", "ダミー", "仮")


@admin.register(Episode)
class EpisodeAdmin(admin.ModelAdmin):
    list_display = ("id", "series", "episode_no", "air_date", "status", "asset")
    list_filter = ("status", "series")
    search_fields = ("title", "series__title")
    raw_id_fields = ("series", "asset")


class AdBreakItemInline(admin.TabularInline):
    model = AdBreakItem
    extra = 0


class AdBreakInline(admin.TabularInline):
    model = AdBreak
    extra = 0
    fields = ("offset_ms", "grid", "duration_ms")


class DistributionRightInline(admin.TabularInline):
    """配信権 (#RIGHTS-02)。VOD 公開はここで自動ゲートされる。"""

    model = DistributionRight
    extra = 0
    fields = (
        "holder",
        "contract_ref",
        "allow_linear",
        "allow_vod",
        "allow_youtube",
        "available_from",
        "available_until",
    )


@admin.register(Program)
class ProgramAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "channel",
        "type",
        "genre",
        "title",
        "start_at",
        "end_at",
        "rating",
        "public_visible",
        "vod_visibility",
        "is_featured",
    )
    # 見逃しトップ featured Hero を一覧でチェック切替。public_visible も一覧で即時切替して
    # テスト番組をまとめて非公開化できるようにする (#5)。
    list_editable = ("public_visible", "is_featured")
    list_filter = (
        "channel",
        "type",
        "genre",
        "rating",
        "public_visible",
        "vod_visibility",
        "is_featured",
    )
    search_fields = ("title",)
    date_hierarchy = "start_at"
    inlines = [AdBreakInline, DistributionRightInline]
    actions = ("hide_from_public",)

    @admin.action(description="選択した番組を非公開にする (public_visible=False)")
    def hide_from_public(self, request, queryset):
        """テスト/仮番組の公開面露出を一括で止める。title で「テスト」等を検索 → 全選択 → 本アクション。"""
        n = queryset.update(public_visible=False)
        self.message_user(request, f"{n} 件を非公開にしました。", messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        # タイトルにテスト語を含む番組を公開のまま保存しようとしたら警告 (出し分け忘れの再発防止)。
        super().save_model(request, obj, form, change)
        title = (obj.title or "").lower()
        if obj.public_visible and any(m.lower() in title for m in _TEST_TITLE_MARKERS):
            self.message_user(
                request,
                f"「{obj.title}」は公開 (public_visible=True) のままです。"
                "テスト/仮番組なら非公開にしてください。",
                messages.WARNING,
            )


@admin.register(AdBreak)
class AdBreakAdmin(admin.ModelAdmin):
    list_display = ("id", "program", "offset_ms", "grid", "duration_ms")
    list_filter = ("grid",)
    inlines = [AdBreakItemInline]


class SeriesPostInline(admin.TabularInline):
    model = SeriesPost
    extra = 0
    fields = ("kind", "title", "is_published", "published_at", "campaign_start", "campaign_end")
    show_change_link = True


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    # 視聴年齢制限 (#BILL-02) はシリーズ単位が主管理。展開 Program の既定レーティングになる。
    list_display = ("id", "channel", "title", "genre", "rating", "is_active")
    list_filter = ("channel", "genre", "rating", "is_active")
    search_fields = ("title",)
    inlines = [SeriesPostInline]


@admin.register(SeriesPost)
class SeriesPostAdmin(admin.ModelAdmin):
    list_display = ("id", "series", "kind", "title", "is_published", "published_at")
    list_filter = ("kind", "is_published", "series__channel")
    search_fields = ("title", "series__title")
    date_hierarchy = "published_at"
