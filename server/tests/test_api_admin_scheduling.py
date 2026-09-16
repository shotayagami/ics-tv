# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-3): 編成タイムライン admin API (読み取り)。

staff_auth ゲート + ジオメトリ (番組の start/end/type/breaks) + ch 一覧。ドラッグ移動/リサイズの
更新は既存 scheduling.edit_views (program_move/resize) を SPA から再利用するため、その業務ロジックは
既存テスト (tests/api/test_scheduling_edit*) の責務。ここは GET の JSON 化を検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from scheduling.models import Program, ProgramType

_TL = "/api/v1/admin/scheduling/{slug}/timeline"


def _program(channel, asset, *, title="編成番組", start_h=2, dur_h=1):
    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=now + timedelta(hours=start_h),
        end_at=now + timedelta(hours=start_h + dur_h),
        asset=asset,
        public_visible=True,
    )


def test_timeline_requires_auth(http_client, channel, db):
    assert http_client.get(_TL.format(slug=channel.slug)).status_code == 401


def test_channels_requires_auth(http_client, db):
    assert http_client.get("/api/v1/admin/scheduling/channels").status_code == 401


def test_channels_list(staff_client, channel, db):
    d = staff_client.get("/api/v1/admin/scheduling/channels").json()
    assert any(c["slug"] == channel.slug and c["name"] == channel.name for c in d)


def test_timeline_lists_programs_geometry(staff_client, channel, asset_ready, db):
    p = _program(channel, asset_ready)
    d = staff_client.get(_TL.format(slug=channel.slug)).json()
    assert d["channel"]["slug"] == channel.slug
    assert "now" in d and "horizon" in d
    assert any(c["slug"] == channel.slug for c in d["channels"])  # ch タブ
    prog = next(x for x in d["programs"] if x["id"] == p.id)
    assert prog["title"] == "編成番組" and prog["type"] == "recorded"
    assert prog["start_at"] and prog["end_at"] and prog["source"] == asset_ready.title
    assert prog["breaks"] == []  # CM枠なし


def test_timeline_window_excludes_far_future(staff_client, channel, asset_ready, db):
    far = _program(channel, asset_ready, title="48h後番組", start_h=48)
    d = staff_client.get(_TL.format(slug=channel.slug)).json()
    assert all(x["id"] != far.id for x in d["programs"])  # now+24h 窓の外は出ない


def test_timeline_unknown_channel_404(staff_client, db):
    assert staff_client.get(_TL.format(slug="nope")).status_code == 404
