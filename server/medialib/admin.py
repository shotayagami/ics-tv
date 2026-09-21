# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin

from medialib import services
from medialib.models import (
    Asset,
    CmBundle,
    CmBundleItem,
    CmCreative,
    CuePoint,
    CueSheet,
    FillerItem,
    FillerPlaylist,
)


@admin.action(description="選択した素材を正規化キューに投入")
def enqueue_normalize(modeladmin, request, queryset):
    """手動トリガ: 選択 Asset を pending に戻して normalize_asset を投入 (失敗素材の再正規化等)。"""
    for asset in queryset:
        services.renormalize(asset)
    modeladmin.message_user(request, f"{queryset.count()} 件を正規化キューに投入しました")


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    # rerun_eligible は changelist から一括トグルできるよう list_editable に (再放送クリップの選別運用)。
    list_display = (
        "id",
        "kind",
        "title",
        "normalize_status",
        "usable_as_filler",
        "rerun_eligible",
        "duration_ms",
        "r2_key",
    )
    list_editable = ("rerun_eligible",)
    list_filter = ("kind", "normalize_status", "usable_as_filler", "rerun_eligible")
    search_fields = ("title", "r2_key", "source_path")
    readonly_fields = ("created_at",)
    actions = [enqueue_normalize]


@admin.register(CmCreative)
class CmCreativeAdmin(admin.ModelAdmin):
    list_display = ("asset", "advertiser", "grid", "aired_count", "max_airings")
    list_filter = ("grid",)
    search_fields = ("advertiser",)


class CmBundleItemInline(admin.TabularInline):
    model = CmBundleItem
    extra = 1


@admin.register(CmBundle)
class CmBundleAdmin(admin.ModelAdmin):
    list_display = ("name",)
    inlines = [CmBundleItemInline]


class CuePointInline(admin.TabularInline):
    model = CuePoint
    extra = 1


@admin.register(CueSheet)
class CueSheetAdmin(admin.ModelAdmin):
    list_display = ("asset", "updated_at")
    inlines = [CuePointInline]


class FillerItemInline(admin.TabularInline):
    model = FillerItem
    extra = 1


@admin.register(FillerPlaylist)
class FillerPlaylistAdmin(admin.ModelAdmin):
    list_display = ("name",)
    inlines = [FillerItemInline]
