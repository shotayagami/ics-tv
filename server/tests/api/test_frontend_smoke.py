# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""フロント動作検証: seed_demo 投入 + 全主要ページが 200 で描画されることの横断 smoke。

ブラウザ不要 (Django test client)。テンプレ/URL/ビューの破損を CI で横断検知する。
seed_demo 自体の妥当性 (8 app 連携 + resolve) もここで担保する。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command


@pytest.fixture
def seeded(db):
    call_command("seed_demo", force=True)  # テストは DEBUG=False。#sec L-12 ガードを force で通す


@pytest.fixture
def demo_client(seeded, client):
    # demo は superuser + staff + DeliveryAccount(internal_admin) = 全 UI を見られる検証ユーザ
    client.force_login(get_user_model().objects.get(username="demo"))
    return client


def test_seed_demo_populates_core_objects(seeded):
    from billing.models import Invoice
    from core.models import Channel
    from playout.models import PlayoutEvent
    from sales.models import Placement

    assert Channel.objects.filter(slug="demo1").exists()
    assert PlayoutEvent.objects.exists()  # resolve が編成→送出イベントを生成
    assert Placement.objects.exists()  # 契約駆動割付が placement を記録
    assert Invoice.objects.exists()


def test_seed_demo_is_idempotent(seeded):
    from core.models import Channel
    from scheduling.models import Program

    call_command("seed_demo", force=True)  # 2 回目
    assert Channel.objects.filter(slug="demo1").count() == 1
    assert Program.objects.filter(title__startswith="DEMO").count() == 1  # EXCLUDE 衝突なし


@pytest.mark.parametrize(
    "path",
    [
        # "/" は管理ホストでは /studio/ へ 302 (test_http_views.test_admin_root_redirects_to_studio_spa)
        "/public/ch/demo1/",
        "/scheduling/ch/demo1/timeline/",
        "/ops/ch/demo1/dashboard/",
        "/billing/",
        "/sales/allocation/",
        "/medialib/",
    ],
)
def test_frontend_pages_render_200(demo_client, path):
    res = demo_client.get(path)
    assert res.status_code == 200, f"{path} -> {res.status_code}"


def test_billing_dashboard_shows_demo_invoice(demo_client):
    body = demo_client.get("/billing/").content.decode("utf-8")
    assert "DEMO-2099-01-0001" in body


def test_allocation_shows_demo_placement(demo_client):
    body = demo_client.get("/sales/allocation/").content.decode("utf-8")
    assert "DEMO" in body  # 割付ビューに DEMO 広告主の CM が出る


def test_channel_page_mounts_player_island(demo_client):
    # #Phase1: 公開プレイヤーは React 島。本体(プレイヤー/共有/コメント)は /static/web/player、
    # OGP(og:title) は引き続き server-render。
    body = demo_client.get("/public/ch/demo1/").content.decode("utf-8")
    assert 'id="player-island"' in body
    assert "/static/web/player/player.js" in body
    assert 'property="og:title"' in body


def test_timeline_does_not_leak_template_comment(demo_client):
    # Django の {# #} は単一行専用 → 複数行コメントが描画漏れする回帰を防ぐ
    body = demo_client.get("/scheduling/ch/demo1/timeline/").content.decode("utf-8")
    assert "extend_program/shorten_program" not in body
    assert "{% comment %}" not in body


def test_footer_shows_app_version(demo_client, settings):
    # フッタ版数は settings.ICSTV_VERSION が単一ソース (ハードコード v=0.1.0 の回帰防止)
    # 管理トップ / は /studio/ へ 302 になったため、base.html を描く管理ページで確認する。
    body = demo_client.get("/billing/").content.decode("utf-8")
    assert f"v={settings.ICSTV_VERSION}" in body
    assert "v=0.1.0" not in body


def test_nav_assets_link_points_to_medialib_dashboard(demo_client):
    # 素材・CM ナビは admin 生 CRUD でなく専用ダッシュボードへ
    body = demo_client.get("/scheduling/ch/demo1/timeline/").content.decode("utf-8")
    assert 'href="/medialib/"' in body
    assert 'href="/admin/medialib/"' not in body
