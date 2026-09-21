# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""実ブラウザ (Playwright) による staff フロー E2E。

ブラウザで admin ログイン → 各 staff UI を操作/描画する。seed_demo でデータを用意。
実行: docker compose --profile test run --rm test pytest -m e2e tests/e2e/test_staff_flows.py
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def demo(db):
    call_command("seed_demo", force=True)  # テストは DEBUG=False。#sec L-12 ガードを force で通す


def _login(page, base_url: str) -> None:
    """Django admin ログインフォームでブラウザログイン (demo は superuser)。"""
    page.goto(f"{base_url}/admin/login/")
    page.fill("input[name=username]", "demo")
    page.fill("input[name=password]", "demo12345")
    page.click("input[type=submit]")  # locale 非依存
    page.wait_for_url("**/admin/")


def test_login_then_billing_shows_invoice(live_server, page, demo):
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/billing/")
    assert "請求" in page.content()
    assert "DEMO-2099-01-0001" in page.content()  # seed の請求書が一覧に出る


def test_sales_band_editor_add_band(live_server, page, demo):
    from sales.models import SpotOrder

    so = SpotOrder.objects.first()
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/sales/orders/{so.id}/bands/")
    assert "線引きエディタ" in page.content()
    # 新規線引き: 月曜 (value=0) + 06:00–09:00 を追加 → HTMX で bands テーブル更新
    page.check("input[name='dow'][value='0']")  # 月 (value=0 → dow_mask bit0)
    page.fill("input[name=start_time]", "06:00")
    page.fill("input[name=end_time]", "09:00")
    page.click("button:has-text('線引き追加')")
    # reload 後、追加した band (月 06:00–09:00) が出る (時刻は |time:"H:i" で固定表示)
    page.wait_for_selector("text=06:00")
    from sales.models import SpotOrderBand

    assert SpotOrderBand.objects.filter(spot_order=so, dow_mask=1, start_time="06:00").exists()


def test_allocation_view_shows_demo_placement(live_server, page, demo):
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/sales/allocation/")
    assert "割付ビュー" in page.content()
    assert "DEMO" in page.content()  # 契約駆動割付で DEMO 広告主の CM が枠に出る


def test_ops_dashboard_renders(live_server, page, demo):
    _login(page, live_server.url)
    page.goto(f"{live_server.url}/ops/ch/demo1/dashboard/")
    # 運行ダッシュボードが 200 で描画 (パネル/見出しが出る)
    assert "DEMO 1ch" in page.content() or "運行" in page.content()
