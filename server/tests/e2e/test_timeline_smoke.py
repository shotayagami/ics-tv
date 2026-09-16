# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Playwright + Django LiveServer による編成タイムラインの E2E smoke。

実行:
  docker compose run --rm web pytest -m e2e tests/e2e/
事前:
  pip install -r requirements-dev.txt && playwright install --with-deps chromium

LiveServerTestCase の代わりに pytest-django の live_server fixture を使う。
pytest-playwright が page fixture を提供 (chromium headless)。

localhost は ICSTV_ADMIN_HOSTS に含まれるため管理 urlconf が適用される。
staff_page fixture (tests/e2e/conftest.py) でスタッフログインを確立する。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


def test_home_navigates_to_timeline(live_server, staff_page, channel):
    staff_page.goto(f"{live_server.url}/scheduling/ch/{channel.slug}/timeline/")
    assert "編成タイムライン" in staff_page.content()


def test_timeline_to_program_create_form(live_server, staff_page, channel):
    staff_page.goto(f"{live_server.url}/scheduling/ch/{channel.slug}/timeline/")
    staff_page.get_by_role("link", name="＋ 番組追加").click()
    staff_page.wait_for_url(f"**/scheduling/ch/{channel.slug}/programs/new/")
    assert "番組追加" in staff_page.content()
    assert staff_page.locator("input[name=title]").count() == 1


def test_program_form_validation_blocks_empty_submit(live_server, staff_page, channel):
    staff_page.goto(f"{live_server.url}/scheduling/ch/{channel.slug}/programs/new/")
    staff_page.get_by_role("button", name="保存").click()
    staff_page.wait_for_url("**/programs/new/")
    assert "番組追加" in staff_page.content()
