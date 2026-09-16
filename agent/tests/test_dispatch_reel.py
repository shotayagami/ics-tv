# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CM バンドル/VT ロールの reel 逐次連結 (main._run_reel) と reel-spawn ゲート
(main._REEL_ACTIONS, _do_take 内) を検証。実 CasparCG 不要。

asyncio.sleep を差し替えて、AUTO コマンドが順序通り・前 clip の尺だけ待って
送られること、空 reel が no-op であること、AMCP 失敗でも止まらないことを確認する。
後半は _do_take が take 成功後に _REEL_ACTIONS 対象 (PLAY_CM_BUNDLE / PLAY_VT) だけ
reel を background spawn することを確認する (#25 §6.5 の一般化)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from icstv.v1 import playout_pb2

from icstv_agent import main
from icstv_agent.caspar import AmcpResult
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.queue_db import QueueDb


class _FakeCaspar:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def amcp(self, command: str):
        self.commands.append(command)
        return None


def test_run_reel_issues_auto_in_order(monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)

    caspar = _FakeCaspar()
    reel = (
        ('LOADBG 1-10 "cm/2002" AUTO', 15000),
        ('LOADBG 1-10 "cm/2003" AUTO', 20000),
    )
    asyncio.run(main._run_reel(caspar, reel))

    assert caspar.commands == [
        'LOADBG 1-10 "cm/2002" AUTO',
        'LOADBG 1-10 "cm/2003" AUTO',
    ]
    assert slept == [15.0, 20.0]


def test_run_reel_empty_is_noop(monkeypatch):
    async def _fake_sleep(sec):
        raise AssertionError("空 reel では sleep しない")

    monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)
    caspar = _FakeCaspar()
    asyncio.run(main._run_reel(caspar, ()))
    assert caspar.commands == []


def test_run_reel_suppresses_amcp_errors(monkeypatch):
    """途中の AMCP 失敗でも reel は止めず次を試行する (best-effort)。"""

    async def _fake_sleep(sec):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", _fake_sleep)

    class _BoomCaspar:
        def __init__(self) -> None:
            self.calls = 0

        async def amcp(self, command: str):
            self.calls += 1
            raise RuntimeError("caspar down")

    caspar = _BoomCaspar()
    asyncio.run(main._run_reel(caspar, (("a", 1000), ("b", 1000))))
    assert caspar.calls == 2


# ---- reel-spawn ゲート (_REEL_ACTIONS, _do_take 内) ----

_CFG = SimpleNamespace(caspar_channel=1, fps=60, transition_ms=0)


class _OkCaspar:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def amcp(self, command: str):
        self.commands.append(command)
        return AmcpResult(code=202, header="OK", body=[])


class _FakeMonitor:
    def on_event_taken(self, action: int, params: dict[str, str]) -> None:
        pass


def _due_entry(db: QueueDb, key: str, ev: playout_pb2.PlayoutEvent) -> dict:
    now = datetime.now(UTC)
    sched = now - timedelta(seconds=1)
    db.upsert_event(key, 1, sched, ev.action, ev.SerializeToString())
    return next(e for e in db.due_for_take(now) if e["idempotency_key"] == key)


def test_reel_actions_includes_cm_bundle_and_play_vt():
    """_REEL_ACTIONS = {PLAY_CM_BUNDLE, PLAY_VT} (#25 §6.5 の一般化)。"""
    # SIM300: _REEL_ACTIONS が大文字名のため ruff が定数と誤認して左右の入れ替えを勧めるが、
    # 「検査対象 == 期待値」の並びのほうが読みやすいのでこのままにする。
    assert main._REEL_ACTIONS == frozenset(  # noqa: SIM300
        {
            playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
            playout_pb2.PLAYOUT_ACTION_PLAY_VT,
        }
    )


def test_do_take_spawns_reel_for_play_cm_bundle(tmp_path, monkeypatch):
    spawned: list[tuple] = []
    monkeypatch.setattr(main, "_spawn_reel", lambda caspar, reel: spawned.append(reel))

    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE)
    ev.params["clips"] = "cm/2001:15000,cm/2002:20000"
    entry = _due_entry(db, "bundle1", ev)

    asyncio.run(
        main._do_take(db, _OkCaspar(), entry, _CFG, "ch", _FakeMonitor(), ChannelMedia("slate/x"))
    )

    assert spawned == [(('LOADBG 1-10 "cm/2002" AUTO', 15000),)]


def test_do_take_spawns_reel_for_play_vt(tmp_path, monkeypatch):
    """PLAY_VT も _REEL_ACTIONS 対象: return_rtmp_url があれば末尾の生復帰 reel を spawn する。"""
    spawned: list[tuple] = []
    monkeypatch.setattr(main, "_spawn_reel", lambda caspar, reel: spawned.append(reel))

    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_VT)
    ev.play_vt.clip = "asset/500"
    ev.play_vt.in_ms = 0
    ev.play_vt.out_ms = 30000
    ev.params["return_rtmp_url"] = "rtmp://127.0.0.1:1935/live/ch1"
    entry = _due_entry(db, "vt1", ev)

    asyncio.run(
        main._do_take(db, _OkCaspar(), entry, _CFG, "ch", _FakeMonitor(), ChannelMedia("slate/x"))
    )

    assert spawned == [(('LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1" AUTO', 30000),)]


def test_do_take_no_reel_spawn_for_play_asset(tmp_path, monkeypatch):
    """PLAY_ASSET は _REEL_ACTIONS 対象外 (reel を持ち得ないので spawn されない、回帰ガード)。"""
    spawned: list[tuple] = []
    monkeypatch.setattr(main, "_spawn_reel", lambda caspar, reel: spawned.append(reel))

    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = "asset/1"
    ev.play_asset.in_ms = 0
    ev.play_asset.out_ms = 1000
    entry = _due_entry(db, "asset1", ev)

    asyncio.run(
        main._do_take(db, _OkCaspar(), entry, _CFG, "ch", _FakeMonitor(), ChannelMedia("slate/x"))
    )

    assert spawned == []
