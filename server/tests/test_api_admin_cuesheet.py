# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-10): キューシート エディタ admin API (本編⊕CM枠の順序リスト)。

staff_auth ゲート + cuepoint 一覧 + 素材内オフセット + 残尺/検証。追加/並べ替え/削除の操作は既存
medialib cuepoint エンドポイント (tests/api/test_cuesheet*) の責務。ここは GET 集約の構造を検証する。
"""

from __future__ import annotations

from medialib.models import CmGrid, CueKind, CuePoint, CueSheet

_URL = "/api/v1/admin/medialib/asset/{id}/cuesheet"


def test_cuesheet_requires_auth(http_client, asset_ready, db):
    assert http_client.get(_URL.format(id=asset_ready.id)).status_code == 401


def test_cuesheet_empty(staff_client, asset_ready, db):
    d = staff_client.get(_URL.format(id=asset_ready.id)).json()
    assert d["asset_id"] == asset_ready.id and d["points"] == []
    assert d["remaining_ms"] == asset_ready.duration_ms  # 本編未設定 → 全尺が残尺


def test_cuesheet_points_and_offset(staff_client, asset_ready, db):
    cue = CueSheet.objects.create(asset=asset_ready)
    CuePoint.objects.create(
        cue_sheet=cue, seq=1, kind=CueKind.CONTENT, duration_ms=600_000
    )  # 10分本編
    CuePoint.objects.create(
        cue_sheet=cue, seq=2, kind=CueKind.AD_BREAK, duration_ms=30_000, grid=CmGrid.G15
    )
    d = staff_client.get(_URL.format(id=asset_ready.id)).json()
    assert len(d["points"]) == 2
    assert d["points"][0]["kind"] == "content" and d["points"][0]["duration"] == "10:00.000"
    cm = d["points"][1]
    assert cm["kind"] == "ad_break" and cm["offset"] == "10:00.000"  # 本編10分後にCM枠
    assert cm["grid"] == CmGrid.G15.value
    assert d["content_total"] == "10:00.000"
    assert d["remaining_ms"] == asset_ready.duration_ms - 600_000
