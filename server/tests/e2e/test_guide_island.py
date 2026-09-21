# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開番組表島 (#Phase2) の E2E (Playwright headless chromium)。

Django(public_base シェル) + React 島 (/static/web/guide) の統合を実ブラウザで検証する:
島マウント + API 駆動のグリッド描画 (列ヘッダ/時間軸/番組ブロック) + 日付ナビ。

前提: フロントを build して server/frontend_dist へ配置済み (live_server の StaticFilesHandler が
/static/web/guide/guide.js を finder から配信する)。実行は test_player_island.py の手順に準ずる。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

# /guide/ + /api/v1/guide を公開 urlconf で解決させる (live_server の localhost を公開ホスト扱い)。
_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


@pytest.fixture
def guide_demo(channel, asset_ready):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="E2E番組ブロック",
        asset=asset_ready,
        start_at=now - timedelta(minutes=20),
        end_at=now + timedelta(minutes=40),
        public_visible=True,
    )
    return channel


@_PUBLIC_HOST
def test_guide_island_renders_grid(live_server, page, guide_demo):
    page.goto(f"{live_server.url}/guide/")
    page.wait_for_selector("#guide-island .grid-inner", timeout=15000)
    assert page.locator("#guide-island .grid-cols .col").count() >= 1
    assert page.locator("#guide-island .axis .h").count() == 24  # 時間軸 0–23時
    page.wait_for_selector("#guide-island .gcol .blk", timeout=5000)  # 現在番組ブロック
    assert "E2E番組ブロック" in page.content()
    # チャンネル帯が grid 上端に密着 (sticky top:0)。overflow:hidden+top:60px の隙間回帰を防ぐ。
    gap = page.evaluate(
        "() => { const g=document.querySelector('#guide-island .grid').getBoundingClientRect();"
        " const c=document.querySelector('#guide-island .grid-cols').getBoundingClientRect();"
        " return c.top - g.top; }"
    )
    assert gap < 5, f"チャンネル帯が {gap}px 下がっている (sticky/overflow 回帰)"


@_PUBLIC_HOST
def test_guide_date_nav_updates_url(live_server, page, guide_demo):
    page.goto(f"{live_server.url}/guide/")
    page.wait_for_selector("#guide-island .grid-inner", timeout=15000)
    page.click("#guide-island .segnav a:has-text('翌日')")
    page.wait_for_function("location.search.includes('date=')", timeout=5000)
    page.wait_for_selector("#guide-island .grid-inner", timeout=5000)  # 翌日グリッドへ再描画


@_PUBLIC_HOST
def test_guide_zoom_changes_height_and_persists(live_server, page, guide_demo):
    """縦ズーム (− / + / ⟲): グリッド高が伸縮し localStorage に保持され再訪でも復元する。"""
    page.goto(f"{live_server.url}/guide/")
    page.wait_for_selector("#guide-island .grid-inner", timeout=15000)
    inner = "#guide-island .grid-inner"
    base_h = page.eval_on_selector(inner, "el => el.getBoundingClientRect().height")

    page.click("#guide-island .zoom button[aria-label='拡大']")
    bigger_h = page.eval_on_selector(inner, "el => el.getBoundingClientRect().height")
    assert bigger_h > base_h, f"拡大で高さが増えていない ({base_h} → {bigger_h})"
    assert page.evaluate("() => localStorage.getItem('icstv.guide.ppm')") is not None

    page.click("#guide-island .zoom button[aria-label='縮小']")
    page.click("#guide-island .zoom button[aria-label='縮小']")
    smaller_h = page.eval_on_selector(inner, "el => el.getBoundingClientRect().height")
    assert smaller_h < bigger_h

    # リロードで保持された密度が復元される (再び拡大前 < 拡大後 の状態に依存せず persist を確認)。
    page.reload()
    page.wait_for_selector(inner, timeout=15000)
    restored_h = page.eval_on_selector(inner, "el => el.getBoundingClientRect().height")
    assert abs(restored_h - smaller_h) < 1.5, (
        f"リロードで密度が復元されない ({smaller_h} → {restored_h})"
    )


@_PUBLIC_HOST
def test_week_zoom_changes_height_and_shares_pref(live_server, page, guide_demo):
    """週間番組表 (server-render + vanilla JS) もズームで高さが伸び、日次と localStorage キーを共有する。"""
    page.goto(f"{live_server.url}/guide/week/")
    page.wait_for_selector(".week-page .grid-inner", timeout=15000)
    inner = ".week-page #weekInner"
    base_h = page.eval_on_selector(inner, "el => el.getBoundingClientRect().height")

    page.click(".week-head .zoom button[aria-label='拡大']")
    bigger_h = page.eval_on_selector(inner, "el => el.getBoundingClientRect().height")
    assert bigger_h > base_h, f"週間ズーム拡大で高さが増えていない ({base_h} → {bigger_h})"
    # 日次 guide と同じキーで保持 = 両ビューで密度が揃う。
    assert page.evaluate("() => localStorage.getItem('icstv.guide.ppm')") is not None
