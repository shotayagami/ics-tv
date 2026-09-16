# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""権利・コンプライアンス URL (管理ホスト config.urls 配下に /rights/ で mount)。"""

from django.urls import path

from rights import views

app_name = "rights"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]
