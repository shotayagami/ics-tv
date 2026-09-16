# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d) のシェル配信。staff 限定 + ディープリンク catch-all。"""

from __future__ import annotations


def test_studio_requires_staff(http_client, db):
    # 匿名は staff_member_required で login へ redirect (302)。
    res = http_client.get("/studio/")
    assert res.status_code == 302


def test_studio_staff_mounts_spa(staff_client, db):
    body = staff_client.get("/studio/").content.decode("utf-8")
    assert 'id="studio-root"' in body
    assert "/static/web/studio/studio.js" in body
    assert "/static/web/studio/studio.css" in body


def test_studio_deeplink_serves_shell(staff_client, db):
    # /studio/<任意> は同じシェルへ (React Router がクライアント側で解決)。
    body = staff_client.get("/studio/rights").content.decode("utf-8")
    assert 'id="studio-root"' in body
    body2 = staff_client.get("/studio/members/stats").content.decode("utf-8")
    assert 'id="studio-root"' in body2


def test_studio_no_comment_leak(staff_client, db):
    # Django {# #} は単一行専用。シェルがテンプレコメントを漏らさない。
    body = staff_client.get("/studio/").content.decode("utf-8")
    assert "{#" not in body
