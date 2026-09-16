# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""exposure_policy (#27) の YTミラー action が main._do_loadbg/_do_take で side-channel
として MirrorController へ委譲され、通常の amcp_planner.plan() 経路を一切通らないことを検証する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from icstv.v1 import playout_pb2

from icstv_agent import main
from icstv_agent.caspar import AmcpResult
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.mirror import MODE_ROUTE, MirrorController
from icstv_agent.queue_db import QueueDb

_CFG = SimpleNamespace(caspar_channel=1, fps=60, transition_ms=0, preroll_sec=5)


class _FakeCaspar:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def amcp(self, command: str) -> AmcpResult:
        self.commands.append(command)
        return AmcpResult(code=202, header="OK", body=[])


class _FakeMonitor:
    def on_event_taken(self, action: int, params: dict[str, str]) -> None:
        pass


def _mirror_ev(action: int, seg: str) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=action)
    ev.params.update({"mirror_seg": seg})
    return ev


def _due_for_take(db: QueueDb, key: str, ev: playout_pb2.PlayoutEvent) -> dict:
    sched = datetime.now(UTC) - timedelta(seconds=2)
    db.upsert_event(key, 1, sched, ev.action, ev.SerializeToString())
    return next(e for e in db.due_for_take(datetime.now(UTC)) if e["idempotency_key"] == key)


def _due_for_loadbg(db: QueueDb, key: str, ev: playout_pb2.PlayoutEvent) -> dict:
    sched = datetime.now(UTC) + timedelta(seconds=1)
    db.upsert_event(key, 1, sched, ev.action, ev.SerializeToString())
    return next(
        e
        for e in db.due_for_loadbg(datetime.now(UTC), preroll_sec=5)
        if e["idempotency_key"] == key
    )


def _outbox_entry(db: QueueDb, key: str) -> playout_pb2.ReportResultRequest:
    entry = next(e for e in db.outbox_iter() if e["idempotency_key"] == key)
    req = playout_pb2.ReportResultRequest()
    req.ParseFromString(entry["payload"])
    return req


def _mirrors(caspar: _FakeCaspar, db: QueueDb) -> dict[str, MirrorController]:
    return {
        "yt_public": MirrorController(
            seg="yt_public",
            mirror_channel=2,
            main_channel=1,
            default_mode=MODE_ROUTE,
            filler_clip_getter=lambda: "filler/site_only_default",
            queue=db,
            caspar=caspar,
        )
    }


def test_loadbg_marks_mirror_action_loaded_without_amcp(tmp_path):
    """YTミラーは背面ロード不要。plan() を経由せず即 mark_loaded で完了扱いにする。"""
    db = QueueDb(tmp_path / "q.db")
    ev = _mirror_ev(playout_pb2.PLAYOUT_ACTION_YT_MIRROR_FILLER, "yt_public")
    entry = _due_for_loadbg(db, "m1", ev)
    caspar = _FakeCaspar()

    done = asyncio.run(
        main._do_loadbg(db, caspar, entry, _CFG, datetime.now(UTC), ChannelMedia("slate/x"))
    )

    assert done is True
    assert caspar.commands == []
    assert db.due_for_loadbg(datetime.now(UTC), preroll_sec=5) == []


def test_take_dispatches_mirror_action_to_controller(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    caspar = _FakeCaspar()
    mirrors = _mirrors(caspar, db)
    ev = _mirror_ev(playout_pb2.PLAYOUT_ACTION_YT_MIRROR_FILLER, "yt_public")
    entry = _due_for_take(db, "m2", ev)

    asyncio.run(
        main._do_take(
            db,
            caspar,
            entry,
            _CFG,
            "ch1",
            _FakeMonitor(),
            ChannelMedia("slate/x"),
            None,
            mirrors,
        )
    )

    assert caspar.commands == ['PLAY 2-10 "filler/site_only_default" LOOP']
    req = _outbox_entry(db, "m2")
    assert req.status == playout_pb2.RESULT_STATUS_DONE
    assert req.note == "yt mirror"
    assert db.due_for_take(datetime.now(UTC)) == []  # 再試行で蒸し返さない


def test_take_mirror_action_without_matching_controller_is_safe(tmp_path):
    """params の mirror_seg に対応する controller が無い (mirrors=None 等) 場合でも
    AMCP を撃たず例外も出さずに畳む (未知ノード構成でも本線を巻き込まない)。"""
    db = QueueDb(tmp_path / "q.db")
    caspar = _FakeCaspar()
    ev = _mirror_ev(playout_pb2.PLAYOUT_ACTION_YT_MIRROR_ROUTE, "yt_public")
    entry = _due_for_take(db, "m3", ev)

    asyncio.run(
        main._do_take(db, caspar, entry, _CFG, "ch1", _FakeMonitor(), ChannelMedia("slate/x"))
    )

    assert caspar.commands == []
    req = _outbox_entry(db, "m3")
    assert req.status == playout_pb2.RESULT_STATUS_DONE


def test_take_mirror_action_never_calls_plan(tmp_path, monkeypatch):
    """YTミラーは通常の amcp_planner.plan() 経路を一切通らないことを保証する回帰テスト。"""
    from icstv_agent import amcp_planner

    def _boom(*args, **kwargs):
        raise AssertionError("plan() should not be called for mirror actions")

    monkeypatch.setattr(amcp_planner, "plan", _boom)

    db = QueueDb(tmp_path / "q.db")
    caspar = _FakeCaspar()
    mirrors = _mirrors(caspar, db)
    ev = _mirror_ev(playout_pb2.PLAYOUT_ACTION_YT_MIRROR_ROUTE, "yt_public")
    entry = _due_for_take(db, "m4", ev)

    asyncio.run(
        main._do_take(
            db,
            caspar,
            entry,
            _CFG,
            "ch1",
            _FakeMonitor(),
            ChannelMedia("slate/x"),
            None,
            mirrors,
        )
    )

    assert caspar.commands == ["PLAY 2-10 route://1"]
