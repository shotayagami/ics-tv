# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OVERLAY_OP (速報/フリーグラフィック) の AMCP 失敗で本線をスレート退避しないことを検証。

速報テロップの自動 hide (CG STOP) が手動 clear 後の空 layer40 で CG エラーを返すと、旧実装は
直近予定の PLAY 失敗として誤って本線をスレート退避していた (2026-06-25)。OVERLAY_OP を
_NO_SLATE_ON_MISS に追加した回帰ガード。対照として PLAY_ASSET の失敗は従来どおりスレートする。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from icstv.v1 import playout_pb2

from icstv_agent import amcp_planner, main
from icstv_agent.caspar import AmcpResult
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.queue_db import QueueDb

_CFG = SimpleNamespace(caspar_channel=1, fps=60, transition_ms=0)
_SLATE_PREFIX = f"PLAY 1-{amcp_planner.LAYER_SLATE}"  # スレート発火コマンドの接頭辞 (= PLAY 1-90)


class _FailCaspar:
    """指定接頭辞のコマンドだけ 404 を返す casparcg スタブ。"""

    def __init__(self, fail_prefixes: tuple[str, ...]) -> None:
        self.commands: list[str] = []
        self._fail = fail_prefixes

    async def amcp(self, command: str) -> AmcpResult:
        self.commands.append(command)
        code = 404 if command.startswith(self._fail) else 202
        return AmcpResult(code=code, header="ERR", body=[])


class _FakeMonitor:
    def on_event_taken(self, action: int, params: dict[str, str]) -> None:
        pass


def _overlay_hide() -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_OVERLAY_OP)
    ev.params["overlay_layer"] = "40"
    ev.params["overlay_op"] = "hide"
    ev.params["overlay_kind"] = "text"
    return ev


def _asset(clip: str) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_ASSET)
    ev.play_asset.clip = clip
    return ev


def _vt(clip: str) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_VT)
    ev.play_vt.clip = clip
    ev.play_vt.in_ms = 0
    ev.play_vt.out_ms = 30000
    return ev


def _recent_due(db: QueueDb, key: str, ev: playout_pb2.PlayoutEvent) -> dict:
    """直近予定 (スレート判定窓 30s 内) の incoming event を due entry 形で返す。"""
    now = datetime.now(UTC)
    sched = now - timedelta(seconds=2)  # actual_at(now) との差 < _SLATE_RECENCY
    db.upsert_event(key, 1, sched, ev.action, ev.SerializeToString())
    return next(e for e in db.due_for_take(now) if e["idempotency_key"] == key)


def _run(db: QueueDb, caspar, entry: dict) -> None:
    asyncio.run(
        main._do_take(db, caspar, entry, _CFG, "ch", _FakeMonitor(), ChannelMedia("slate/x"))
    )


def test_overlay_op_failure_does_not_slate(tmp_path):
    db = QueueDb(tmp_path / "q.db")
    entry = _recent_due(db, "ov1", _overlay_hide())
    # 空 layer40 への CG STOP 失敗を模擬 (CG コマンドを 404)。
    caspar = _FailCaspar(("CG ",))
    _run(db, caspar, entry)

    assert any(c.startswith("CG 1-40 STOP") for c in caspar.commands)  # hide は試行された
    assert not any(c.startswith(_SLATE_PREFIX) for c in caspar.commands)  # 本線スレートは出さない


def test_play_vt_failure_does_not_slate(tmp_path):
    """PLAY_VT (生番組内 VT ロール) の PLAY 失敗でも本線をスレート退避しない (#25 §6.5)。

    VT は CM 同様の挿入コンテンツで、未到達/失敗時に前面 (直前の生) を覆い隠すべきではない。
    """
    db = QueueDb(tmp_path / "q.db")
    entry = _recent_due(db, "vt1", _vt("asset/500"))
    caspar = _FailCaspar(("PLAY 1-10",))  # 本線 take を失敗させる
    _run(db, caspar, entry)

    assert not any(c.startswith(_SLATE_PREFIX) for c in caspar.commands)


def test_asset_failure_still_slates(tmp_path):
    """対照: 本編 (PLAY_ASSET) の直近 PLAY 失敗は従来どおりスレート退避する。"""
    db = QueueDb(tmp_path / "q.db")
    entry = _recent_due(db, "as1", _asset("asset/42"))
    caspar = _FailCaspar(("PLAY 1-10",))  # 本線 take を失敗させる (スレートの PLAY 1-90 は通す)
    _run(db, caspar, entry)

    assert any(c.startswith(_SLATE_PREFIX) for c in caspar.commands)  # スレート退避する
