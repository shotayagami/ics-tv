# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* ホスト分離 (#27)。他ホストからcreator専用パスへ到達不能、creator.*からは
studio/公開/放送ルートへ到達不能なことを確認する (test_host_split.py と同型)。
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings

PUBLIC = "tv.example.com"
ADMIN = "admin.example.com"
CREATOR = "creator.example.com"

_HOSTS = {
    "ALLOWED_HOSTS": ["testserver", PUBLIC, ADMIN, CREATOR],
    "ICSTV_ADMIN_HOSTS": [ADMIN],
    "ICSTV_CREATOR_HOSTS": [CREATOR],
}


@pytest.fixture
def web():
    return Client()


@override_settings(**_HOSTS)
def test_creator_host_serves_login(web, db):
    res = web.get("/login/", HTTP_HOST=CREATOR)
    assert res.status_code == 200
    assert "クリエイター" in res.content.decode("utf-8")


@override_settings(**_HOSTS)
def test_creator_host_hides_studio_and_public_routes(web, channel):
    for url in ("/admin/", "/studio/", "/guide/", f"/ch/{channel.slug}/"):
        res = web.get(url, HTTP_HOST=CREATOR)
        assert res.status_code == 404, f"{url} should be 404 on creator host, got {res.status_code}"


@override_settings(**_HOSTS)
def test_creator_host_root_is_dashboard_not_public_home(web, db):
    # "/" は #27 PR8 で creator dashboard (creator_login_required) になった。
    # 公開トップ (core.public_home) ではないことを、未ログイン時のログイン誘導で確認する。
    res = web.get("/", HTTP_HOST=CREATOR)
    assert res.status_code == 302 and res.url.startswith("/login/")


@override_settings(**_HOSTS)
def test_other_hosts_hide_creator_routes(web, db):
    for host in (PUBLIC, ADMIN, "testserver"):
        for url in ("/login/", "/invite/dummy-token/", "/auth/google/start/"):
            res = web.get(url, HTTP_HOST=host)
            assert res.status_code == 404, f"{url} should be 404 on {host}, got {res.status_code}"


@override_settings(**_HOSTS)
def test_creator_host_admin_api_unreachable(web, db):
    # studio ninja API (internal_admin_api) は creator.* にマウントしない
    res = web.get("/api/v1/admin/fanclub/creators", HTTP_HOST=CREATOR)
    assert res.status_code == 404
