# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開/管理ホスト分離 (#7)。公開ホストは urls_public で管理ルートが 404、公開ページが見える。"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings

PUBLIC = "tv.example.com"
ADMIN = "admin.example.com"

# 公開ホストは ADMIN_HOSTS に含めない → urls_public。ADMIN は含める → フル。
_HOSTS = {
    "ALLOWED_HOSTS": ["testserver", PUBLIC, ADMIN],
    "ICSTV_ADMIN_HOSTS": [ADMIN, "testserver"],
}


@pytest.fixture
def web():
    return Client()


@override_settings(**_HOSTS)
def test_public_host_serves_public_home(web, channel):
    res = web.get("/", HTTP_HOST=PUBLIC)
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "チャンネル" in body and 'id="home-island"' in body  # #Phase2b: カードは島
    # アプリ名はトップで明示。YouTube Data API の利用開示は API を実際に使う studio 側
    # (channel_settings の OAuth 連携カード) へ移設したので公開トップには出さない (視聴者は API に触れない)。
    assert "ICS-TV" in body and "YouTube Data API" not in body
    # プライバシー/規約は footer から公開のまま到達可 (Google 審査が未認証アクセスを要求するため)。
    assert "/privacy/" in body and "/terms/" in body


@override_settings(**_HOSTS)
def test_public_host_guide_and_channel(web, channel):
    assert web.get("/guide/", HTTP_HOST=PUBLIC).status_code == 200
    assert web.get(f"/ch/{channel.slug}/", HTTP_HOST=PUBLIC).status_code == 200


@override_settings(**_HOSTS)
def test_public_host_privacy_and_terms(web, db):
    p = web.get("/privacy/", HTTP_HOST=PUBLIC)
    assert p.status_code == 200 and "Limited Use" in p.content.decode("utf-8")
    t = web.get("/terms/", HTTP_HOST=PUBLIC)
    assert t.status_code == 200 and "利用規約" in t.content.decode("utf-8")


@override_settings(**_HOSTS)
def test_public_host_hides_admin_routes(web, channel):
    # 編成/運用/admin は公開 urlconf に存在しない → 404 (302 redirect でもない)
    for url in (
        f"/scheduling/ch/{channel.slug}/timeline/",
        f"/ops/ch/{channel.slug}/dashboard/",
        "/admin/",
        "/medialib/",
    ):
        res = web.get(url, HTTP_HOST=PUBLIC)
        assert res.status_code == 404, f"{url} should be 404 on public host, got {res.status_code}"


@override_settings(**_HOSTS)
def test_admin_host_has_full_routes(web, channel):
    # 管理ホストでは編成ルートが解決する (未認証なので login へ 302、404 ではない)
    res = web.get(f"/scheduling/ch/{channel.slug}/timeline/", HTTP_HOST=ADMIN)
    assert res.status_code == 302  # staff_member_required → login
    # 管理トップ / は新 studio SPA (/studio/) へ 302 リダイレクト (旧運用ダッシュボードを置換)
    root = web.get("/", HTTP_HOST=ADMIN)
    assert root.status_code == 302 and root.headers["Location"] == "/studio/"


@override_settings(**_HOSTS)
def test_public_channel_switcher_when_multi(web, channel, db):
    from core.models import Channel

    Channel.objects.create(name="2ch", slug="ch2", enabled=True)
    # #Phase1: チャンネルタブは島が /api/v1/channels から描画する。
    data = web.get("/api/v1/channels", HTTP_HOST=PUBLIC).json()
    assert {c["slug"] for c in data} >= {"ch1", "ch2"}  # 他チャンネルへの切替
