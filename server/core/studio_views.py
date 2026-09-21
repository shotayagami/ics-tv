# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d) のシェル配信。

公開フロントの島と違い studio は「真の SPA」(React Router でクライアントルーティング)。
Django はシェル (#studio-root + studio.js) を返すだけで、画面は React が描く。staff 限定なので
SEO 不要・session+CSRF をそのまま使う。ディープリンク (/studio/rights 等) も同じシェルへ流す
(catch-all URL → React Router が解決)。@ensure_csrf_cookie で SPA の非 GET 用に csrftoken を配る。
"""

from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@staff_member_required
@ensure_csrf_cookie
def studio_app(request: HttpRequest, rest: str = "") -> HttpResponse:
    return render(request, "studio/app.html")
