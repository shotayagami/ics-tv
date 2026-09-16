# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#23 番組専用枠ダッシュボード/手動操作 + archive_watch_url の専用枠優先。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from youtube.models import ProgramBroadcast, YoutubeBroadcastPreset, YoutubeConfig, YtSlotStatus

pytestmark = pytest.mark.django_db


def _program(channel, asset, *, start, end, **kw):
    from scheduling.models import Program, ProgramType

    return Program.objects.create(
        channel=channel,
        type=kw.pop("type", ProgramType.LIVE),
        title=kw.pop("title", "番組"),
        start_at=start,
        end_at=end,
        asset=asset if kw.pop("with_asset", False) else None,
        live_source=kw.pop("live_source", None),
        **kw,
    )


# ---- archive_watch_url の専用枠優先 ----


def test_archive_watch_url_prefers_dedicated(channel, asset_ready):
    from core.models import LiveSource
    from youtube.archive import archive_watch_url

    YoutubeConfig.objects.create(channel=channel, privacy="public")
    src = LiveSource.objects.create(name="src", rtmp_app="live", rtmp_key="k")
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(hours=2),
        end=now - timedelta(hours=1),
        live_source=src,
    )
    preset = YoutubeBroadcastPreset.objects.create(name="p", privacy="public")
    ProgramBroadcast.objects.create(
        program=prog, preset=preset, broadcast_id="DEDICATED1", status=YtSlotStatus.COMPLETE
    )
    assert archive_watch_url(prog) == "https://www.youtube.com/watch?v=DEDICATED1"


def test_archive_watch_url_dedicated_private_not_exposed(channel, asset_ready):
    from core.models import LiveSource
    from youtube.archive import archive_watch_url

    YoutubeConfig.objects.create(channel=channel, privacy="public")
    src = LiveSource.objects.create(name="src2", rtmp_app="live", rtmp_key="k2")
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(hours=2),
        end=now - timedelta(hours=1),
        live_source=src,
    )
    preset = YoutubeBroadcastPreset.objects.create(name="pp", privacy="private")
    ProgramBroadcast.objects.create(
        program=prog, preset=preset, broadcast_id="SECRET", status=YtSlotStatus.COMPLETE
    )
    assert archive_watch_url(prog) == ""


# ---- ダッシュボード + 手動操作 ----


def test_dashboard_renders(staff_client, channel):
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/youtube/dedicated/")
    assert res.status_code == 200
    assert "番組専用枠" in res.content.decode("utf-8")


def test_create_now_requires_preset(staff_client, channel, asset_ready):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="x",
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        asset=asset_ready,
        youtube_dedicated=True,  # フラグはあるが preset 無し
    )
    res = staff_client.post(f"/admin-ui/youtube/dedicated/{prog.id}/create/")
    assert res.status_code == 302
    assert not ProgramBroadcast.objects.filter(program=prog).exists()


def test_create_now_creates_with_preset(staff_client, channel, asset_ready, monkeypatch):
    from scheduling.models import Program, ProgramType

    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    preset = YoutubeBroadcastPreset.objects.create(name="p", category_id=24)
    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="八神翔太",
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        asset=asset_ready,
        youtube_dedicated=True,
        youtube_preset=preset,
    )
    monkeypatch.setattr("youtube.tasks.insert_broadcast", lambda ch, **kw: "BCnew")
    monkeypatch.setattr("youtube.tasks.apply_broadcast_preset", lambda *a, **k: None)

    res = staff_client.post(f"/admin-ui/youtube/dedicated/{prog.id}/create/")
    assert res.status_code == 302
    pb = ProgramBroadcast.objects.get(program=prog)
    assert pb.broadcast_id == "BCnew"
    assert pb.manual is True
    assert pb.status == YtSlotStatus.READY


def test_transition_go_live(staff_client, channel, asset_ready, monkeypatch):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="p",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.READY)
    calls = []
    monkeypatch.setattr(
        "youtube.api.transition_broadcast", lambda ch, bid, t: calls.append((bid, t))
    )
    res = staff_client.post(
        f"/admin-ui/youtube/program-broadcast/{pb.id}/transition/", {"target": "live"}
    )
    assert res.status_code == 302
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.LIVE
    assert calls == [("BC", "live")]


def test_checklist_save(staff_client, channel, asset_ready):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="p",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    preset = YoutubeBroadcastPreset.objects.create(name="p")
    pb = ProgramBroadcast.objects.create(program=prog, preset=preset, broadcast_id="BC")
    res = staff_client.post(
        f"/admin-ui/youtube/program-broadcast/{pb.id}/checklist/",
        {"checklist_ai_disclosure": "1", "checklist_paid_promotion": "1"},
    )
    assert res.status_code == 302
    pb.refresh_from_db()
    assert pb.checklist_state["ai_disclosure"] is True
    assert pb.checklist_state["paid_promotion"] is True
    assert pb.checklist_state["age_restriction"] is False
