# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 0: フロント SPA シェル (/app/) のスモーク。

frontend_dist の有無で 200/503 が変わる (CI test では未ビルド=503)。どちらでも 500 に
ならず、ビルド済みなら /static/web/ を参照する html を返すことを確認する。
"""


def test_spa_shell_no_500(client):
    resp = client.get("/app/")
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        body = resp.content.decode()
        assert "/static/web/" in body
