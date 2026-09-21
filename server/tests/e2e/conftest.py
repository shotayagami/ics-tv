# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E 専用 conftest — staff ログイン済み Playwright ページ fixture。"""

from __future__ import annotations

import pytest


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
