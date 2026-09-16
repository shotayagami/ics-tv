# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""休止スレート (resolver 管轄 off_air) の有効期限ガードを検証。

休止中は resolver が周期ごとに PLAY_SLATE を「今」の時刻で再発行する。休止明けの直前に
発行された 1 発は gRPC 到達 + ディスパッチ遅延の間に休止明けの CLEAR_SLATE に追い越され、
そのまま撃つと復帰済みの本線 (layer 10) の上へスレート (layer 90) を再点灯させて固着する
(2026-07-21 06:00: 05:59:59 発行のスレートが 06:00:01 に実行され、運用者が手動解除する
08:21 まで 2h21m スレート固着)。params["until"] を過ぎた off_air スレートは撃たない。
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

_CFG = SimpleNamespace(caspar_channel=1, fps=60, transition_ms=0)
_SLATE_CMD = 'PLAY 1-90 "slate/please_wait" LOOP'


class _FakeCaspar:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def amcp(self, command: str) -> AmcpResult:
        self.commands.append(command)
        return AmcpResult(code=202, header="OK", body=[])


class _FakeMonitor:
    def __init__(self) -> None:
        self.taken: list[int] = []

    def on_event_taken(self, action: int, params: dict[str, str]) -> None:
        self.taken.append(action)


def _slate(**params: str) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent(action=playout_pb2.PLAYOUT_ACTION_PLAY_SLATE)
    ev.params.update(params)
    return ev


def _due(db: QueueDb, key: str, ev: playout_pb2.PlayoutEvent) -> dict:
    """scheduled_at を過去に置き due_for_take の entry 形で返す。"""
    sched = datetime.now(UTC) - timedelta(seconds=2)
    db.upsert_event(key, 1, sched, ev.action, ev.SerializeToString())
    return next(e for e in db.due_for_take(datetime.now(UTC)) if e["idempotency_key"] == key)


def _run_take(db: QueueDb, caspar, entry: dict, monitor=None) -> None:
    asyncio.run(
        main._do_take(
            db,
            caspar,
            entry,
            _CFG,
            "ch1",
            monitor or _FakeMonitor(),
            ChannelMedia("slate/please_wait"),
        )
    )


def _outbox_status(db: QueueDb, key: str) -> int:
    entry = next(e for e in db.outbox_iter() if e["idempotency_key"] == key)
    req = playout_pb2.ReportResultRequest()
    req.ParseFromString(entry["payload"])
    return req.status


def test_expired_off_air_slate_is_not_sent(tmp_path):
    """until を過ぎた off_air スレートは AMCP を撃たず SKIPPED で畳む (本不具合の根治)。"""
    db = QueueDb(tmp_path / "q.db")
    until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    entry = _due(db, "s1", _slate(off_air="True", until=until, loop="True"))

    caspar = _FakeCaspar()
    monitor = _FakeMonitor()
    _run_take(db, caspar, entry, monitor)

    assert caspar.commands == []  # スレートを再点灯させない
    assert monitor.taken == []  # feed monitor の slate 調停も動かさない
    assert db.due_for_take(datetime.now(UTC)) == []  # executed 済み (再試行で蒸し返さない)
    assert _outbox_status(db, "s1") == playout_pb2.RESULT_STATUS_SKIPPED


def test_off_air_slate_before_until_is_sent(tmp_path):
    """休止中 (until 未到来) の off_air スレートは従来どおり撃つ。"""
    db = QueueDb(tmp_path / "q.db")
    until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    entry = _due(db, "s2", _slate(off_air="True", until=until, loop="True"))

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)

    assert caspar.commands == [_SLATE_CMD]
    assert _outbox_status(db, "s2") == playout_pb2.RESULT_STATUS_DONE


def test_manual_slate_has_no_deadline(tmp_path):
    """運用者の手動/緊急スレート (off_air param 無し) は期限を持たず常に撃つ。"""
    db = QueueDb(tmp_path / "q.db")
    entry = _due(db, "s3", _slate(reason="manual_emergency", interrupt="True"))

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)

    assert caspar.commands == [_SLATE_CMD]


def test_unparsable_until_falls_back_to_sending(tmp_path):
    """until が壊れていたら判定不能 → 撃つ (スレートを出さない側に倒さない)。"""
    db = QueueDb(tmp_path / "q.db")
    entry = _due(db, "s4", _slate(off_air="True", until="not-a-timestamp"))

    caspar = _FakeCaspar()
    _run_take(db, caspar, entry)

    assert caspar.commands == [_SLATE_CMD]
