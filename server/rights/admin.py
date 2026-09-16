# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin

from rights.models import DistributionRight


@admin.register(DistributionRight)
class DistributionRightAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "program",
        "holder",
        "allow_linear",
        "allow_vod",
        "allow_youtube",
        "available_from",
        "available_until",
    )
    list_filter = ("allow_vod", "allow_youtube", "allow_linear")
    raw_id_fields = ("program",)
    search_fields = ("program__title", "holder", "contract_ref")
