# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""feed_monitor 状態機械の検証 (docs/operations.md O2/O6)。実 MediaMTX/CasparCG 不要。

I/O (caspar/mediamtx/send_interrupt) を fake で注入し、tick() を駆動して
断→スレート / 復帰→自動戻り / 手動スレート抑止 / フラップ停止 / トグル を確認する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from icstv.v1 import playout_pb2

from icstv_agent.channel_media import ChannelMedia
from icstv_agent.feed_monitor import IDLE, LOST, OK, FeedMonitor

K = playout_pb2.ReportInterruptRequest.InterruptKind
CUT_LIVE = playout_pb2.PLAYOUT_ACTION_CUT_LIVE
PLAY_SLATE = playout_pb2.PLAYOUT_ACTION_PLAY_SLATE
CLEAR_SLATE = playout_pb2.PLAYOUT_ACTION_CLEAR_SLATE
PLAY_ASSET = playout_pb2.PLAYOUT_ACTION_PLAY_ASSET
PLAY_FILLER = playout_pb2.PLAYOUT_ACTION_PLAY_FILLER
LIVE = {"rtmp_url": "rtmp://127.0.0.1:1935/live/ch1"}


class FakeCaspar:
    def __init__(self) -> None:
        self.cmds: list[str] = []

    async def amcp(self, command: str):
        self.cmds.append(command)
        return None


class FakeMtx:
    def __init__(self, ready) -> None:
        self.ready = ready

    async def publisher_ready(self, path):
        return self.ready


def _mk(ready=True, auto_return=True, clock=None):
    sent: list[tuple[int, str]] = []

    async def send(kind, detail):
        sent.append((kind, detail))

    caspar = FakeCaspar()
    mtx = FakeMtx(ready)
    fm = FeedMonitor(
        caspar=caspar,
        mediamtx=mtx,
        send_interrupt=send,
        channel=1,
        channel_media=ChannelMedia("slate/please_wait"),
        lost_ticks=2,
        hysteresis_ticks=2,
        flap_max=3,
        auto_return=auto_return,
        clock=clock,
    )
    return fm, caspar, mtx, sent


def _tick(fm):
    asyncio.run(fm.tick())


def test_idle_when_not_live():
    fm, caspar, _mtx, sent = _mk(ready=False)
    _tick(fm)
    assert fm.feed_state == IDLE
    assert not caspar.cmds and not sent


def test_feed_drop_triggers_slate_after_lost_ticks():
    fm, caspar, _mtx, sent = _mk(ready=False)
    fm.on_event_taken(CUT_LIVE, LIVE)
    _tick(fm)  # lost=1: まだ退避しない
    assert not fm.slate_active and not sent
    _tick(fm)  # lost=2: 退避
    assert fm.slate_active and fm.slate_by_monitor and fm.feed_state == LOST
    assert any("PLAY 1-90" in c for c in caspar.cmds)
    assert sent[-1][0] == K.INTERRUPT_KIND_SLATE_ON


def test_recovery_after_hysteresis():
    fm, caspar, mtx, sent = _mk(ready=False)
    fm.on_event_taken(CUT_LIVE, LIVE)
    _tick(fm)
    _tick(fm)  # 退避
    assert fm.slate_active
    mtx.ready = True
    _tick(fm)  # ok=1: まだ戻さない (hysteresis=2)
    assert fm.slate_active
    _tick(fm)  # ok=2: 自動復帰
    assert not fm.slate_active and not fm.slate_by_monitor and fm.feed_state == OK
    assert any("CLEAR 1-90" in c for c in caspar.cmds)
    assert any('LOADBG 1-10 "rtmp://127.0.0.1:1935/live/ch1"' in c for c in caspar.cmds)
    assert any(k == K.INTERRUPT_KIND_FEED_RESTORED for k, _ in sent)


def test_manual_slate_blocks_auto_return():
    fm, _caspar, mtx, _sent = _mk(ready=False)
    fm.on_event_taken(CUT_LIVE, LIVE)
    _tick(fm)
    _tick(fm)  # monitor 退避 (slate_by_monitor=True)
    fm.on_event_taken(PLAY_SLATE, {})  # operator が手動スレートを被せる
    assert fm.manual_slate_active
    mtx.ready = True
    for _ in range(5):
        _tick(fm)
    assert fm.slate_active  # 手動のため自動復帰しない
    fm.on_event_taken(CLEAR_SLATE, {})  # 手動解除で調停状態もクリア
    assert not fm.manual_slate_active and not fm.slate_active and not fm.slate_by_monitor


def test_auto_return_off_no_recovery():
    fm, _caspar, mtx, _sent = _mk(ready=False, auto_return=False)
    fm.on_event_taken(CUT_LIVE, LIVE)
    _tick(fm)
    _tick(fm)  # 退避
    mtx.ready = True
    for _ in range(5):
        _tick(fm)
    assert fm.slate_active  # トグル OFF → 自動復帰しない


def test_agent_control_reenable_clears_suspend():
    fm, *_ = _mk()
    fm.auto_return = False
    fm.auto_return_suspended = True
    fm.on_agent_control(True)
    assert fm.auto_return and not fm.auto_return_suspended


def test_flap_limit_suspends_auto_return():
    ticks = [0]
    base = datetime(2026, 6, 11, 0, 0, 0, tzinfo=UTC)

    def clock():
        # 1 tick = 1 秒 (flap_window 600s 内に収まる → 短時間の連続フラップ扱い)
        from datetime import timedelta

        return base + timedelta(seconds=ticks[0])

    fm, _caspar, mtx, sent = _mk(ready=False, clock=clock)
    fm.on_event_taken(CUT_LIVE, LIVE)
    # drop→recover を flap_max(3) 回まで → 4 回目の復帰でサスペンド
    for _ in range(4):
        mtx.ready = False
        ticks[0] += 1
        _tick(fm)
        ticks[0] += 1
        _tick(fm)  # 退避
        mtx.ready = True
        ticks[0] += 1
        _tick(fm)
        ticks[0] += 1
        _tick(fm)  # 復帰 (hysteresis=2)
    assert fm.auto_return_suspended
    assert any(k == K.INTERRUPT_KIND_AUTO_RETURN_SUSPENDED for k, _ in sent)


def test_play_asset_clears_live_active():
    fm, _caspar, _mtx, _sent = _mk(ready=False)
    fm.on_event_taken(CUT_LIVE, LIVE)
    assert fm.live_active
    fm.on_event_taken(PLAY_ASSET, {"clip": "asset/1"})
    assert not fm.live_active
    _tick(fm)  # 録画中は idle 判定
    assert fm.feed_state == IDLE


class FakeRecordingManager:
    """record_live 連携の検証用フェイク。実 MediaMTX/gRPC/R2 I/O は行わない。"""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []

    async def start(self, path: str, channel_slug: str, program_id: int) -> None:
        self.started.append((path, channel_slug, program_id))

    async def stop_and_upload(self, path: str) -> None:
        self.stopped.append(path)


def _mk_with_recording(ready=True):
    sent: list[tuple[int, str]] = []

    async def send(kind, detail):
        sent.append((kind, detail))

    caspar = FakeCaspar()
    mtx = FakeMtx(ready)
    recording = FakeRecordingManager()
    fm = FeedMonitor(
        caspar=caspar,
        mediamtx=mtx,
        send_interrupt=send,
        channel=1,
        channel_media=ChannelMedia("slate/please_wait"),
        channel_slug="ch1",
        lost_ticks=2,
        hysteresis_ticks=2,
        flap_max=3,
        recording=recording,
    )
    return fm, recording


async def _take(fm, action, params):
    """on_event_taken を呼び、spawn された録画 background task の完了まで待つ (テスト用)。

    本番では on_event_taken は常に稼働中の event loop (dispatch loop 内 _do_take) から
    呼ばれるため、テストも同じ前提 (loop 稼働中に呼ぶ) で駆動する。
    """
    fm.on_event_taken(action, params)
    tasks = list(fm._recording_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def test_cut_live_with_record_param_starts_recording():
    fm, recording = _mk_with_recording()

    async def scenario():
        params = {**LIVE, "record": "1", "program_id": "42"}
        await _take(fm, CUT_LIVE, params)

    asyncio.run(scenario())
    assert recording.started == [("live/ch1", "ch1", 42)]
    assert fm.recording_active


def test_cut_live_without_record_param_does_not_start_recording():
    fm, recording = _mk_with_recording()
    asyncio.run(_take(fm, CUT_LIVE, LIVE))  # record パラメタなし
    assert recording.started == []
    assert not fm.recording_active


def test_play_asset_stops_active_recording():
    fm, recording = _mk_with_recording()

    async def scenario():
        params = {**LIVE, "record": "1", "program_id": "42"}
        await _take(fm, CUT_LIVE, params)
        assert fm.recording_active
        await _take(fm, PLAY_ASSET, {"clip": "asset/1"})

    asyncio.run(scenario())
    assert recording.stopped == ["live/ch1"]
    assert not fm.recording_active


def test_play_filler_stops_active_recording():
    fm, recording = _mk_with_recording()

    async def scenario():
        params = {**LIVE, "record": "1", "program_id": "7"}
        await _take(fm, CUT_LIVE, params)
        await _take(fm, PLAY_FILLER, {"clip": "filler/1"})

    asyncio.run(scenario())
    assert recording.stopped == ["live/ch1"]


def test_cut_live_recording_failure_does_not_raise():
    """録画開始が例外を投げても on_event_taken 自体・以後の呼び出しには一切伝播しない。"""

    class BoomRecording:
        async def start(self, path, channel_slug, program_id):
            raise RuntimeError("mediamtx unreachable")

        async def stop_and_upload(self, path):
            raise RuntimeError("should not be called")

    caspar = FakeCaspar()
    mtx = FakeMtx(True)
    fm = FeedMonitor(
        caspar=caspar,
        mediamtx=mtx,
        send_interrupt=lambda k, d: asyncio.sleep(0),
        channel=1,
        channel_media=ChannelMedia("slate/please_wait"),
        channel_slug="ch1",
        recording=BoomRecording(),
    )

    async def scenario():
        params = {**LIVE, "record": "1", "program_id": "42"}
        # 例外を投げず即座に戻る (fire-and-forget)。background task 内で例外が握り潰される。
        await _take(fm, CUT_LIVE, params)

    asyncio.run(scenario())
    assert not fm.recording_active  # start() 例外のため active化されない
