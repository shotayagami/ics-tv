# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開トップ ライブカード島 (#Phase2b) の E2E (Playwright headless chromium)。

Django(public_base シェル + 下部 SSR) + React 島 (/static/web/home) の統合を実ブラウザで検証する:
島マウント + API 駆動の hero/カード描画 (現在番組・ライブプレビュー video) + SSR セクション共存。
実映像デコードは bundled chromium に codec 無しのため非検証 (DOM/データ層のみ)。
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
def home_demo(channel, asset_ready):
    from scheduling.models import Program, ProgramType

    channel.cf_playback_hls_url = "https://example.invalid/hls/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="ホームE2E番組",
        genre="アニメ",  # → 下部「ジャンルから探す」(SSR) も出す
        asset=asset_ready,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
    )
    return channel


@_PUBLIC_HOST
def test_home_island_renders_hero_and_cards(live_server, page, home_demo):
    page.goto(f"{live_server.url}/")
    page.wait_for_selector("#home-island .hero", timeout=15000)
    assert page.locator("#home-island .chcard").count() >= 1
    # 現在番組タイトル (hero h1 + card ttl) が API 駆動で描画される
    assert "ホームE2E番組" in page.content()
    # ライブプレビュー video 要素 (hls_url あり)
    assert page.locator("#home-island .hero video").count() == 1
    assert page.locator("#home-island .chcard .thumb video").count() >= 1
    # 下部 SSR セクションが島と共存して出る
    assert "ジャンルから探す" in page.content()
