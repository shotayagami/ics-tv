# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理 (staff) の URL。管理ホスト (config.urls) からのみ include される。"""

from django.urls import path

from members import staff_views

app_name = "members_admin"

urlpatterns = [
    path("stats/", staff_views.stats, name="stats"),
]
