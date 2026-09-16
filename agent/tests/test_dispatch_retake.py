# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""casparcg 再接続時の現行イベント貼り直し (main._retake_current) を検証 (#7)。

casparcg 再起動で本線が消えると、executed 済みの現行イベントは due_for_take に再び
乗らず次の予定 TAKE まで黒落ちする。再接続検知時に現行を PLAY ... LOOP で貼り直すこと、
対象が無ければ何もしないこと、本線を止めないため best-effort であることを確認する。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from icstv.v1 import playout_pb2

from icstv_agent import main
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.queue_db import QueueDb

_CFG = SimpleNamespace(caspar_channel=1)


class _FakeCaspar:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def amcp(self, command: str):
        self.commands.append(command)
        return None


def _executed(db: QueueDb, key: str, seq: int, ev: playout_pb2.PlayoutEvent) -> None:
    from datetime import UTC, datetime

    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event(key, seq, at, ev.action, ev.SerializeToString())
    db.mark_executed(key, at)


def test_retake_current_replays_asset_as_loop(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = "asset/42"
    _executed(db, "k1", 1, ev)

    caspar = _FakeCaspar()
    asyncio.run(main._retake_current(db, caspar, _CFG))
    assert caspar.commands == ['PLAY 1-10 "asset/42" LOOP']


def test_retake_current_noop_when_no_main_event(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    # CM のみ executed → 本線の持続状態ではないので貼り直さない
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_CM)
    ev.params["clip"] = "cm/1"
    _executed(db, "cm", 1, ev)

    caspar = _FakeCaspar()
    asyncio.run(main._retake_current(db, caspar, _CFG))
    assert caspar.commands == []


def test_retake_current_picks_latest_main_event(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    old = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    old.play_asset.clip = "asset/old"
    new = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_FILLER)
    new.play_filler.clip = "filler/now"
    _executed(db, "old", 1, old)
    # executed_at は mark_executed の when で決まる。新しい方を後の時刻で記録する。
    from datetime import UTC, datetime

    later = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    db.upsert_event("new", 2, later, new.action, new.SerializeToString())
    db.mark_executed("new", later)

    caspar = _FakeCaspar()
    asyncio.run(main._retake_current(db, caspar, _CFG))
    assert caspar.commands == ['PLAY 1-10 "filler/now" LOOP']


def test_retake_current_best_effort_on_amcp_error(tmp_path):
    """AMCP 失敗でも例外を伝播させない (本線送出ループを落とさない)。"""
    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_CUT_LIVE)
    ev.cut_live.rtmp_url = "rtmp://x/live/key"
    _executed(db, "live", 1, ev)

    class _Boom:
        async def amcp(self, command: str):
            raise RuntimeError("caspar down")

    asyncio.run(main._retake_current(db, _Boom(), _CFG))  # 例外が出なければ合格


class _FlakyCaspar:
    """未接続で起動し、初回 connect() で接続する casparcg スタブ (再起動→再接続の模擬)。"""

    def __init__(self) -> None:
        self._connected = False
        self.commands: list[str] = []
        self.connect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        self.connect_calls += 1
        self._connected = True
        return True

    async def amcp(self, command: str):
        self.commands.append(command)
        return None


def test_dispatch_loop_retakes_current_on_reconnect(tmp_path, monkeypatch):
    """未接続起動→初回接続を「切断→接続」遷移として検知し、現行を再 take する (loop 結線)。"""
    monkeypatch.setattr(main, "_DISPATCH_TICK_SEC", 0.005)
    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = "asset/live"
    _executed(db, "k1", 1, ev)

    caspar = _FlakyCaspar()  # 未接続で起動
    cfg = SimpleNamespace(caspar_channel=1, preroll_sec=5)
    # due イベント無し → _do_take は未使用。再接続経路が slate_active を見るのでスタブに持たせる。
    monitor = SimpleNamespace(slate_active=False)
    cm = ChannelMedia("slate/please_wait")  # due 無しで未使用だが loop 引数に必要

    async def _drive():
        task = asyncio.create_task(main._dispatch_loop(db, caspar, "ch", cfg, monitor, cm))
        for _ in range(50):  # 数 tick 回して再 take の発火を待つ
            if caspar.commands:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    # 再接続で 1 度だけ現行を貼り直す (接続後の通常 tick では due 無しで追加発火しない)。
    assert caspar.commands == ['PLAY 1-10 "asset/live" LOOP']


class _RecyclingCaspar:
    """接続済みで起動し、drop() 後の初回 connect() で必ず成功する casparcg スタブ。

    計画的リサイクルの実挙動 (AMCP ポートが即座に再バインドされ、再接続が一度も
    失敗しない) の模擬。_FlakyCaspar は未接続起動なので、この「接続済み→切断→即復帰」
    の経路は覆えない。
    """

    def __init__(self) -> None:
        self._connected = True
        self.commands: list[str] = []
        self.connect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def drop(self) -> None:
        self._connected = False

    async def connect(self) -> bool:
        self.connect_calls += 1
        self._connected = True
        return True

    async def amcp(self, command: str):
        self.commands.append(command)
        return None


def test_dispatch_loop_retakes_when_reconnect_never_fails(tmp_path, monkeypatch):
    """再接続が初回で成功しても現行を貼り直す (2026-08-21 の 35 分黒落ちの再発防止)。

    「一度は接続に失敗した」ことを再 take の条件にしていると、casparcg が即座に AMCP を
    再バインドしたリサイクルで取りこぼし、次の予定 TAKE まで黒のまま放置される。
    """
    monkeypatch.setattr(main, "_DISPATCH_TICK_SEC", 0.005)
    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_FILLER)
    ev.play_filler.clip = "filler/19"
    _executed(db, "k1", 1, ev)

    caspar = _RecyclingCaspar()  # 接続済みで起動
    cfg = SimpleNamespace(caspar_channel=1, preroll_sec=5)
    monitor = SimpleNamespace(slate_active=False)
    cm = ChannelMedia("slate/please_wait")

    async def _drive():
        task = asyncio.create_task(main._dispatch_loop(db, caspar, "ch", cfg, monitor, cm))
        await asyncio.sleep(0.02)  # 接続済みのまま数 tick 回す
        assert caspar.commands == []  # 平常時は貼り直さない
        caspar.drop()  # ここで casparcg がリサイクルされる
        for _ in range(50):
            if caspar.commands:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    assert caspar.commands == ['PLAY 1-10 "filler/19" LOOP']
    assert caspar.connect_calls == 1  # 再接続は一度も失敗していない


# ---- スレート層の復帰 (#7) ----


def test_retake_slate_replays_slate_layer():
    """スレート表示中なら slate layer を貼り直す。"""
    caspar = _FakeCaspar()
    cfg = SimpleNamespace(caspar_channel=1)
    cm = ChannelMedia("slate/please_wait")
    monitor = SimpleNamespace(slate_active=True)

    assert asyncio.run(main._retake_slate(caspar, cfg, cm, monitor)) is True
    assert caspar.commands == ['PLAY 1-90 "slate/please_wait" LOOP']


def test_retake_slate_noop_when_not_active():
    """スレートが出ていない (通常放送中) なら何もしない。"""
    caspar = _FakeCaspar()
    cfg = SimpleNamespace(caspar_channel=1)
    cm = ChannelMedia("slate/please_wait")
    monitor = SimpleNamespace(slate_active=False)

    assert asyncio.run(main._retake_slate(caspar, cfg, cm, monitor)) is False
    assert caspar.commands == []


def test_retake_slate_uses_manifest_clip():
    """clip は goto_slate と同じ channel_media.slate_clip を使う (manifest 由来を優先)。"""
    caspar = _FakeCaspar()
    cfg = SimpleNamespace(caspar_channel=1)
    cm = ChannelMedia("slate/please_wait")
    cm.set_slate_clip("slate/off_air_2026")
    monitor = SimpleNamespace(slate_active=True)

    asyncio.run(main._retake_slate(caspar, cfg, cm, monitor))
    assert caspar.commands == ['PLAY 1-90 "slate/off_air_2026" LOOP']


def test_retake_slate_best_effort_on_amcp_error():
    """AMCP 失敗でも例外を伝播させない (本線送出ループを落とさない)。"""

    class _Boom:
        async def amcp(self, command: str):
            raise RuntimeError("caspar down")

    cfg = SimpleNamespace(caspar_channel=1)
    cm = ChannelMedia("slate/please_wait")
    monitor = SimpleNamespace(slate_active=True)
    assert asyncio.run(main._retake_slate(_Boom(), cfg, cm, monitor)) is False


def test_dispatch_loop_retakes_slate_with_main_on_reconnect(tmp_path, monkeypatch):
    """再接続では本線とスレートの両方を復帰させる。

    本線だけ戻すと、休止帯や緊急退避中に隠されていた本線が露出する (2026-08-21 に判明)。
    """
    monkeypatch.setattr(main, "_DISPATCH_TICK_SEC", 0.005)
    db = QueueDb(tmp_path / "q.db")
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_FILLER)
    ev.play_filler.clip = "filler/19"
    _executed(db, "k1", 1, ev)

    caspar = _RecyclingCaspar()
    cfg = SimpleNamespace(caspar_channel=1, preroll_sec=5)
    monitor = SimpleNamespace(slate_active=True)  # 休止帯 = スレート表示中
    cm = ChannelMedia("slate/please_wait")

    async def _drive():
        task = asyncio.create_task(main._dispatch_loop(db, caspar, "ch", cfg, monitor, cm))
        await asyncio.sleep(0.02)
        caspar.drop()
        for _ in range(50):
            if len(caspar.commands) >= 2:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    assert caspar.commands == [
        'PLAY 1-10 "filler/19" LOOP',
        'PLAY 1-90 "slate/please_wait" LOOP',
    ]
