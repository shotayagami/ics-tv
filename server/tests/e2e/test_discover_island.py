# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開ディスカバリ島 (#Phase2c) の E2E (Playwright headless chromium)。

search (ライブ検索) / browse (ジャンルタブ) / vod (一覧) の島マウント + API 駆動描画を検証する。
実行は test_player_island.py の手順に準ずる (frontend build → server/frontend_dist 配置)。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


@pytest.fixture
def discover_demo(channel, asset_ready):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="ディスカバリE2E番組",
        genre="アニメ",
        asset=asset_ready,
        start_at=now + timedelta(days=1),
        end_at=now + timedelta(days=1, hours=1),
        public_visible=True,
    )
    return channel


@_PUBLIC_HOST
def test_search_live(live_server, page, discover_demo):
    page.goto(f"{live_server.url}/search/")
    page.wait_for_selector("#discover-island .sr-input", timeout=15000)
    page.fill("#discover-island .sr-input", "ディスカバリ")  # 入力→デバウンスでライブ検索
    page.wait_for_selector("#discover-island .sr-list", timeout=8000)
    assert "ディスカバリE2E番組" in page.content()


@_PUBLIC_HOST
def test_browse_tabs(live_server, page, discover_demo):
    page.goto(f"{live_server.url}/browse/")
    page.wait_for_selector("#discover-island .segnav", timeout=15000)
    page.click("#discover-island .segnav a:has-text('アニメ')")
    page.wait_for_function("location.search.includes('genre=')", timeout=5000)
    page.wait_for_selector("#discover-island .vodcard", timeout=8000)
    assert "ディスカバリE2E番組" in page.content()


@_PUBLIC_HOST
def test_vod_mounts(live_server, page, channel):
    page.goto(f"{live_server.url}/vod/")
    page.wait_for_selector("#discover-island", timeout=15000)
    assert "見逃し配信" in page.content()
