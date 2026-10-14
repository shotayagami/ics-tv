# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E 専用 conftest — staff ログイン済み Playwright ページ fixture。"""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _settle_page_before_teardown(request):
    """テストの終了前に、ページの通信が落ち着くのを待つ。

    SPA は操作の後に一覧を再取得する。テストが成功表示だけを見て終わると、この再取得が
    進行中のまま DB の初期化 (TRUNCATE) に入り、サーバー側の SELECT とロックを取り合って
    デッドロックする ("Database test_icstv couldn't be flushed")。

    page を fixture 内で取得することで、この teardown が page の破棄より前に走る。
    待ちが失敗しても (常時通信するページなど) テストの結果は変えない。
    """
    page = request.getfixturevalue("page") if "page" in request.fixturenames else None
    yield
    if page is not None:
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=5000)


@pytest.fixture
def staff_page(db, page, live_server):
    """Django superuser を作成して Django admin 経由でログインした Playwright page を返す。

    localhost は ICSTV_ADMIN_HOSTS に含まれるため管理 urlconf が適用される。
    @staff_member_required ビューに Django admin ログインでアクセス可能になる。
    """
    from django.contrib.auth import get_user_model

    get_user_model().objects.create_superuser("e2estaff", "e2e@example.com", "e2epass123")

    page.goto(f"{live_server.url}/admin/login/")
    page.fill("input[name=username]", "e2estaff")
    page.fill("input[name=password]", "e2epass123")
    page.locator("input[type=submit]").click()
    page.wait_for_url("**/admin/**", timeout=10000)
    return page
