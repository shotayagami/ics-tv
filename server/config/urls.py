# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ICS-TV ルーティング。Phase 1 最小: home + scheduling + admin。"""

from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic.base import RedirectView

from api.api import internal_admin_api, public_api
from api.spa import spa_index
from core.studio_views import studio_app

urlpatterns = [
    # 管理ホスト (studio.*) のトップは新 studio SPA へ。旧「運用ダッシュボード」(core.views.home)
    # は ops.* (放送コンソール) と SPA ダッシュボードに置き換わったため、ログイン直後を SPA に着地
    # させる。公開 tv.* は urls_public (public_home)・ops.* は urls_ops と別 urlconf なので無影響。
    path("", RedirectView.as_view(url="/studio/", permanent=False), name="home"),
    path("admin/", admin.site.urls),
    # JSON API (django-ninja)。管理/内部ホストは公開島 API + 内部連携 + 管理 SPA の両方を
    # 同一 prefix にマウント (#sec M-4)。django-ninja の api-root は path("") で catch-all では
    # ないため、未マッチは Django resolver が次の include へバックトラックする。
    path("api/v1/", public_api.urls),
    path("api/v1/", internal_admin_api.urls),
    # フロント React アプリ (Phase 0 / Option B)。SPA シェル + クライアントルーティング。
    path("app/", spa_index, name="spa"),
    path("app/<path:rest>", spa_index),
    # studio 管理 SPA (#Phase2d)。staff 限定。/studio/* は全てシェルへ (React Router が解決)。
    re_path(r"^studio/", studio_app, name="studio_app"),
    path("scheduling/", include("scheduling.urls", namespace="scheduling")),
    # 営放 請求 UI (#6 Phase B)。staff (経理) 専用
    path("billing/", include("billing.urls", namespace="billing")),
    # 番組予算管理 (#5 P4 / 制作費=支払 AP)。staff (経理/管理) 専用
    # 営放 営業 UI (#6 / ui-sales.md 線引き・割付)。staff (営業/管理者) 専用
    path("sales/", include("sales.urls", namespace="sales")),
    # 素材・CM 管理 UI (#7 / ui.md)。staff 専用。正規化状態・CM 在庫・考査
    path("medialib/", include("medialib.urls", namespace="medialib")),
    # 権利・コンプライアンス (#RIGHTS-02)。staff 専用。配信権
    path("rights/", include("rights.urls", namespace="rights")),
    # 会員統計 (staff 専用 / 管理ホストのみ)。視聴者向け会員ページは公開ホスト urls_public 側
    path("members-admin/", include("members.staff_urls")),
    # core/urls.py が ops/ と public/ を提供 (prefix なしで include)
    path("", include("core.urls", namespace="core")),
]
