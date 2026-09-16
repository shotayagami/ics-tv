# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 0: ninja API skeleton の疎通。

テスト client の既定ホスト=testserver は ICSTV_ADMIN_HOSTS なので config.urls
(管理ホスト urlconf) で解決される。/api/v1/ は config.urls にもマウント済み。
"""


def test_health_ok(client):
    resp = client.get("/api/v1/health/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "icstv-api"
    # 版数はビルド時 ARG。未ビルド (テスト) は default "dev"。
    assert "version" in data
