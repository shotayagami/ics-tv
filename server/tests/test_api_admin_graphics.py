# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2e-1): 自動グラフィック graphic_cues admin API (owner 別 CG キュー)。

staff_auth ゲート + owner(series) の GraphicCue 一覧 + 要約。追加/削除の操作は既存
scheduling.graphic_views (tests/api/test_graphic*) の責務。ここは GET 集約の構造を検証する。
"""

from __future__ import annotations

from scheduling.models import GraphicCue, GraphicKind, Series

_URL = "/api/v1/admin/scheduling/{slug}/graphic-cues/series/{oid}"


def test_graphic_cues_requires_auth(http_client, channel, db):
    s = Series.objects.create(channel=channel, title="t")
    assert http_client.get(_URL.format(slug=channel.slug, oid=s.id)).status_code == 401


def test_graphic_cues_list(staff_client, channel, db):
    s = Series.objects.create(channel=channel, title="シリーズA")
    GraphicCue.objects.create(
        series=s,
        layer=30,
        kind=GraphicKind.TEXT,
        data={"text": "テロップ"},
        show_at_ms=5000,
        hide_at_ms=15000,
        seq=0,
    )
    d = staff_client.get(_URL.format(slug=channel.slug, oid=s.id)).json()
    assert d["owner"] == "series" and d["owner_id"] == s.id and d["title"]
    row = d["cues"][0]
    assert row["layer"] == 30 and row["kind"] == "text" and row["summary"] == "テロップ"
    assert row["show_s"] == 5.0 and row["hide_s"] == 15.0


def test_graphic_cues_bad_owner_404(staff_client, channel, db):
    assert (
        staff_client.get(
            f"/api/v1/admin/scheduling/{channel.slug}/graphic-cues/program/999999"
        ).status_code
        == 404
    )
