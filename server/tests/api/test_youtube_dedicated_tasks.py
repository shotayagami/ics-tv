# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#23 番組専用枠 beat (generate_dedicated_broadcasts / rotate_dedicated) の単体テスト。

YouTube API ラッパー (insert_broadcast / apply_broadcast_preset / create_dedicated_stream /
transition_broadcast / livestream_active) を monkeypatch し、選別ロジックと状態遷移を検証する。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from youtube import tasks
from youtube.models import ProgramBroadcast, YoutubeBroadcastPreset, YtSlotStatus

pytestmark = pytest.mark.django_db


def _program(channel, asset, *, start, end, **kw):
    from scheduling.models import Program, ProgramType

    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=kw.pop("title", "番組"),
        start_at=start,
        end_at=end,
        asset=asset,
        **kw,
    )


def _preset(**kw):
    kw.setdefault("name", "preset")
    return YoutubeBroadcastPreset.objects.create(**kw)


def _stub_create(monkeypatch, *, insert_id="BC", capture=None):
    def fake_insert(ch, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        return insert_id

    monkeypatch.setattr("youtube.tasks.insert_broadcast", fake_insert)
    monkeypatch.setattr("youtube.tasks.apply_broadcast_preset", lambda *a, **k: None)


def test_generate_creates_dedicated_broadcast(channel, asset_ready, monkeypatch):
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    preset = _preset(category_id=24, title_template="【専用】{program}")
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        title="八神翔太",
        youtube_dedicated=True,
        youtube_preset=preset,
    )
    cap: dict = {}
    _stub_create(monkeypatch, insert_id="BCdone", capture=cap)

    stats = tasks.generate_dedicated_broadcasts(channel.id)
    assert stats["created"] == 1
    pb = ProgramBroadcast.objects.get(program=prog)
    assert pb.broadcast_id == "BCdone"
    assert pb.status == YtSlotStatus.READY
    assert cap["stream_id"] == "LS2"  # 2本目 liveStream に bind
    assert cap["scheduled_end"] == prog.end_at
    assert cap["title"] == "【専用】八神翔太"
    assert pb.checklist_state  # preset 既定から初期化


def test_generate_provisions_second_stream_when_missing(channel, asset_ready, monkeypatch):
    assert not channel.youtube_livestream_id_2
    preset = _preset(category_id=24)
    now = timezone.now()
    _program(
        channel,
        asset_ready,
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        youtube_dedicated=True,
        youtube_preset=preset,
    )

    def fake_create_stream(ch):
        ch.youtube_livestream_id_2 = "LSnew"
        ch.save(update_fields=["youtube_livestream_id_2"])

    monkeypatch.setattr("youtube.tasks.create_dedicated_stream", fake_create_stream)
    _stub_create(monkeypatch)

    stats = tasks.generate_dedicated_broadcasts(channel.id)
    assert stats["provisioned"] == 1
    assert stats["created"] == 1


def test_generate_skips_non_dedicated_and_no_preset(channel, asset_ready, monkeypatch):
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    # 専用フラグ無し
    _program(
        channel,
        asset_ready,
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        youtube_dedicated=False,
    )
    # 専用フラグありだが preset 未解決
    _program(
        channel,
        asset_ready,
        start=now + timedelta(hours=3),
        end=now + timedelta(hours=4),
        youtube_dedicated=True,
    )
    _stub_create(monkeypatch)

    stats = tasks.generate_dedicated_broadcasts(channel.id)
    assert stats["created"] == 0
    assert stats["no_preset"] == 1
    assert ProgramBroadcast.objects.count() == 0


def test_generate_is_idempotent(channel, asset_ready, monkeypatch):
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    preset = _preset(category_id=24)
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        youtube_dedicated=True,
        youtube_preset=preset,
    )
    ProgramBroadcast.objects.create(
        program=prog, preset=preset, broadcast_id="X", status=YtSlotStatus.READY
    )
    _stub_create(monkeypatch)

    stats = tasks.generate_dedicated_broadcasts(channel.id)
    assert stats["created"] == 0
    assert stats["skipped"] == 1


def test_rotate_dedicated_to_live(channel, asset_ready, monkeypatch):
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=55),
        youtube_dedicated=True,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.READY)
    monkeypatch.setattr("youtube.tasks.livestream_active", lambda ch, lid=None: True)
    calls = []
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast", lambda ch, bid, t: calls.append((bid, t))
    )

    stats = tasks.rotate_dedicated(channel.id)
    assert stats["to_live"] == 1
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.LIVE
    assert calls == [("BC", "live")]


def test_rotate_dedicated_blocked_when_stream_inactive(channel, asset_ready, monkeypatch):
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=55),
        youtube_dedicated=True,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.READY)
    monkeypatch.setattr("youtube.tasks.livestream_active", lambda ch, lid=None: False)
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast",
        lambda *a, **k: pytest.fail("inactive stream では transition しない"),
    )

    stats = tasks.rotate_dedicated(channel.id)
    assert stats["blocked"] == 1
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.READY


def _http_error(status: int, reason: str = "backendError"):
    import json

    import httplib2
    from googleapiclient.errors import HttpError

    resp = httplib2.Response({"status": status, "reason": reason})
    content = json.dumps(
        {"error": {"errors": [{"reason": reason}], "code": status, "message": reason}}
    ).encode()
    return HttpError(resp, content)


def test_rotate_dedicated_transient_5xx_keeps_ready(channel, asset_ready, monkeypatch):
    """5xx は ERROR 確定にせず READY のまま、翌分の rotate が再試行する (rotate_slots と同じ)。"""
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=55),
        youtube_dedicated=True,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.READY)
    monkeypatch.setattr("youtube.tasks.livestream_active", lambda ch, lid=None: True)

    def boom(ch, bid, t):
        raise _http_error(503, "SERVICE_UNAVAILABLE")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)

    stats = tasks.rotate_dedicated(channel.id)
    assert stats["deferred"] == 1
    assert stats["errors"] == 0
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.READY
    assert "503" in pb.error


def test_rotate_dedicated_complete_reconciles_deleted(channel, asset_ready, monkeypatch):
    """終了済み/削除済み broadcast への complete は invalidTransition でも COMPLETE 追認。"""
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(hours=2),
        end=now - timedelta(minutes=1),
        youtube_dedicated=True,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.LIVE)

    def boom(ch, bid, t):
        raise _http_error(403, "invalidTransition")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)
    monkeypatch.setattr("youtube.tasks.broadcast_lifecycle", lambda ch, bid: None)

    stats = tasks.rotate_dedicated(channel.id)
    assert stats["to_complete"] == 1
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.COMPLETE


def test_rotate_dedicated_missed_program_marked_error(channel, asset_ready, monkeypatch):
    """番組終了まで live 化されなかった READY は ERROR で可視化。"""
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(hours=2),
        end=now - timedelta(hours=1),
        youtube_dedicated=True,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.READY)
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast",
        lambda *a, **k: pytest.fail("番組時間外の枠は transition しない"),
    )

    stats = tasks.rotate_dedicated(channel.id)
    assert stats["missed"] == 1
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.ERROR
    assert "番組時間を通過" in pb.error


def test_rotate_dedicated_to_complete(channel, asset_ready, monkeypatch):
    channel.youtube_livestream_id_2 = "LS2"
    channel.save(update_fields=["youtube_livestream_id_2"])
    now = timezone.now()
    prog = _program(
        channel,
        asset_ready,
        start=now - timedelta(hours=2),
        end=now - timedelta(minutes=1),
        youtube_dedicated=True,
    )
    pb = ProgramBroadcast.objects.create(program=prog, broadcast_id="BC", status=YtSlotStatus.LIVE)
    calls = []
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast", lambda ch, bid, t: calls.append((bid, t))
    )

    stats = tasks.rotate_dedicated(channel.id)
    assert stats["to_complete"] == 1
    pb.refresh_from_db()
    assert pb.status == YtSlotStatus.COMPLETE
    assert calls == [("BC", "complete")]
