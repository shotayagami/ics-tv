# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""🔴 放送コンソール専用 urlconf (リファクタ Phase 1 / ops.* ホスト)。

ops.* (放送当直の専用ホスト・CF Access 前段・モバイルファースト) はこの urlconf で動く
(core.middleware.HostUrlconfMiddleware が割当)。放送コンソール SPA + その API + 認証だけを
公開する。編成/納品/経理 等の管理 UI は urlconf に存在せず、SPA の catch-all が受ける
(= studio.* のフル機能は出ない = 機能的に到達不能)。ninja API と admin は staff 認証付きで共有。
送出操作 (/ops/ HTMX) は Phase 1.2 で必要分のみ追加する。
"""

from django.contrib import admin
from django.urls import path, re_path

from api.api import internal_admin_api, public_api
from core.ops_views import ops_app
from core.urls import OPS_OPERATIONS

urlpatterns = [
    # staff 認証 (staff_member_required のログインリダイレクト先) + 保守用 admin。
    path("admin/", admin.site.urls),
    # 放送コンソールが叩く JSON API (ninja)。admin ops status 等。放送ホストは CF Access 背後
    # なので公開島 + 内部/管理の両 API をマウントする (#sec M-4 は公開 tv.* のみ制限)。
    path("api/v1/", public_api.urls),
    path("api/v1/", internal_admin_api.urls),
    # 送出操作 (/ops/ch/<slug>/...)。studio.* と共有する正本 (core.urls.OPS_OPERATIONS) を
    # 放送コンソールから form-POST できるよう放送ホストにも載せる (リファクタ Phase 1.2)。
    *OPS_OPERATIONS,
    # 放送コンソール SPA シェル。ops.* ルート直下 (basename 無し)。上記以外は全てここへ
    # → React Router が解決 (/ch1 等のディープリンクも同じシェル)。
    re_path(r"^", ops_app),
]
