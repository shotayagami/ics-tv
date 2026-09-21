# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sales app の Django admin (社内運用)。マスタ + 契約 + 台帳の参照/編集。"""

from __future__ import annotations

from django.contrib import admin

from sales.models import (
    AdContract,
    Advertiser,
    Agency,
    Airing,
    Industry,
    MakeGood,
    Placement,
    RateCard,
    SpotOrder,
    TimeBandRank,
)


@admin.register(Industry)
class IndustryAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name")
    search_fields = ("code", "name")


@admin.register(Advertiser)
class AdvertiserAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "industry", "screening_status", "is_active")
    list_filter = ("screening_status", "is_active", "industry")
    search_fields = ("name",)


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "commission_rate", "is_active")


@admin.register(TimeBandRank)
class TimeBandRankAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "dow", "start_time", "end_time", "rank")
    list_filter = ("channel", "rank", "dow")


@admin.register(RateCard)
class RateCardAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "rank", "unit_seconds", "price", "effective_from")
    list_filter = ("channel", "rank")


@admin.register(AdContract)
class AdContractAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "kind",
        "title",
        "advertiser",
        "agency",
        "period_start",
        "period_end",
        "status",
    )
    list_filter = ("kind", "status")
    search_fields = ("title",)
    raw_id_fields = ("advertiser", "agency")


@admin.register(SpotOrder)
class SpotOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "contract",
        "channel",
        "period_start",
        "period_end",
        "target_count",
        "unit_seconds",
        "unit_price",
    )
    raw_id_fields = ("contract", "channel")


@admin.register(Placement)
class PlacementAdmin(admin.ModelAdmin):
    list_display = ("id", "ad_break_item", "match_kind", "spot_order", "sponsorship")
    list_filter = ("match_kind",)
    raw_id_fields = ("ad_break_item", "spot_order", "sponsorship")


@admin.register(Airing)
class AiringAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "aired_at",
        "channel",
        "cm_asset",
        "program_title",
        "spot_order",
        "sponsorship",
        "billed",
    )
    list_filter = ("billed", "channel")
    raw_id_fields = ("playout_event", "cm_asset", "spot_order", "sponsorship", "program")


@admin.register(MakeGood)
class MakeGoodAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "spot_order", "sponsorship", "missed_playout_event")
    list_filter = ("status",)
    raw_id_fields = ("spot_order", "sponsorship", "missed_playout_event", "replacement_airing")
