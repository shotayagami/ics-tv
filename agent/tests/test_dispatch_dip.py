# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""フィラー↔番組境界の黒フェード (main._do_take の dip 結線) を検証。

フィラーと実編成を差し替える瞬間のハードカットが視聴体験を損なうため、本線(N-10)を
黒+無音へ落としきってからテイクし、テイク後に通常へ戻して番組をフェードインする。
フィラー境界 (フィラー↔非フィラー) のときだけ適用し、番組同士/CM のハードカット既定や
transition_ms=0 (無効) では従来どおりハードカットすることを確認する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from icstv.v1 import playout_pb2

from icstv_agent import main
from icstv_agent.caspar import AmcpResult
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.queue_db import QueueDb

# fps=60 / transition_ms=100ms → 6 フレーム。テストの sleep を 0.1s に抑えつつ端数の無い値。
_CFG = SimpleNamespace(caspar_channel=1, fps=60, transition_ms=100)
_NOW = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)


class _FakeCaspar:
    def __init__(self, *, fail_play: bool = False) -> None:
        self.commands: list[str] = []
        self._fail_play = fail_play

    async def amcp(self, command: str) -> AmcpResult:
        self.commands.append(command)
        code = 404 if (self._fail_play and command.startswith("PLAY")) else 202
        return AmcpResult(code=code, header="OK", body=[])


class _FakeMonitor:
    def on_event_taken(self, action: int, params: dict[str, str]) -> None:
        pass


def _filler(clip: str) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_FILLER)
    ev.play_filler.clip = clip
    return ev


def _asset(clip: str) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = clip
    return ev


def _seed_executed(db: QueueDb, key: str, seq: int, ev: playout_pb2.PlayoutEvent) -> None:
    """直前にオンエア済み (current_main_event が返す outgoing) を仕込む。"""
    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event(key, seq, at, ev.action, ev.SerializeToString())
    db.mark_executed(key, at)


def _due_incoming(db: QueueDb, key: str, seq: int, ev: playout_pb2.PlayoutEvent) -> dict:
    """これからテイクする incoming event を due_for_take の entry 形で返す。"""
    sched = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    db.upsert_event(key, seq, sched, ev.action, ev.SerializeToString())
    return next(e for e in db.due_for_take(_NOW) if e["idempotency_key"] == key)


def _run_take(db: QueueDb, caspar, entry: dict, cfg=_CFG) -> None:
    asyncio.run(
        main._do_take(db, caspar, entry, cfg, "ch", _FakeMonitor(), ChannelMedia("slate/x"))
    )


_DIP_OUT = ["MIXER 1-10 OPACITY 0.0 6 linear", "MIXER 1-10 VOLUME 0.0 6 linear"]
_DIP_IN = ["MIXER 1-10 OPACITY 1.0 6 linear", "MIXER 1-10 VOLUME 1.0 6 linear"]


def test_filler_to_program_dips_to_black(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    _seed_executed(db, "f0", 1, _filler("filler/loop"))  # オンエア中=フィラー
    entry = _due_incoming(db, "a1", 2, _asset("asset/42"))  # 差し替える実番組

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)

    # フェードアウト → テイク → フェードイン の順
    assert caspar.commands == [*_DIP_OUT, "PLAY 1-10", *_DIP_IN]
    assert db.due_for_take(_NOW) == []  # executed 済みに


def test_program_to_filler_dips_to_black(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    _seed_executed(db, "a0", 1, _asset("asset/1"))  # オンエア中=番組
    entry = _due_incoming(db, "f1", 2, _filler("filler/night"))  # フィラーへ戻る

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)
    assert caspar.commands == [*_DIP_OUT, "PLAY 1-10", *_DIP_IN]


def test_program_to_program_hard_cut(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    _seed_executed(db, "a0", 1, _asset("asset/1"))  # 番組→番組はフィラー境界でない
    entry = _due_incoming(db, "a1", 2, _asset("asset/2"))

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)
    assert caspar.commands == ["PLAY 1-10"]  # 黒フェードなし (既定どおりハードカット)


def test_filler_to_filler_hard_cut(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    _seed_executed(db, "f0", 1, _filler("filler/a"))  # フィラー同士のローテーション
    entry = _due_incoming(db, "f1", 2, _filler("filler/b"))

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)
    assert caspar.commands == ["PLAY 1-10"]


def test_transition_ms_zero_disables_dip(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    _seed_executed(db, "f0", 1, _filler("filler/loop"))
    entry = _due_incoming(db, "a1", 2, _asset("asset/42"))

    caspar = _FakeCaspar()
    _run_take(
        db,
        caspar,
        entry,
        cfg=SimpleNamespace(caspar_channel=1, fps=60, transition_ms=0),
    )
    assert caspar.commands == ["PLAY 1-10"]  # 無効化で従来のハードカット


def test_dip_take_failure_restores_levels(tmp_path):
    """黒フェード中にテイク失敗したら本線を即時 1.0 へ戻し、黒+無音のまま残さない。"""
    db = QueueDb(tmp_path / "q.db")
    _seed_executed(db, "f0", 1, _filler("filler/loop"))
    entry = _due_incoming(db, "a1", 2, _asset("asset/42"))

    caspar = _FakeCaspar(fail_play=True)
    _run_take(db, caspar, entry)

    # フェードアウト → PLAY(失敗) → 即時復帰 (トゥイーン無し)。フェードインはしない。
    assert caspar.commands == [
        *_DIP_OUT,
        "PLAY 1-10",
        "MIXER 1-10 OPACITY 1.0",
        "MIXER 1-10 VOLUME 1.0",
    ]
