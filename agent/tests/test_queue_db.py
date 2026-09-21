# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""queue_db: LOADBG 候補の絞り込み (停波の根本=実行済みイベントの無限再試行を防ぐ回帰テスト)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from icstv_agent.queue_db import QueueDb


def _db(tmp_path):
    return QueueDb(tmp_path / "queue.db")


def test_due_for_loadbg_excludes_executed(tmp_path):
    """take 段で実行 (失敗含む) 済みのイベントは loaded_at=NULL でも LOADBG 候補に出さない。

    これが無いと、実在しない clip を参照する孤児/失敗イベントが毎 tick 404→goto_slate を
    繰り返し本編を覆い隠す (2026-06-13 の停波の根本)。
    """
    db = _db(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("k1", 1, now - timedelta(minutes=1), 1, b"")
    assert "k1" in {e["idempotency_key"] for e in db.due_for_loadbg(now, preroll_sec=5)}

    db.mark_executed("k1", now)  # take 失敗でも executed_at が入る
    assert "k1" not in {e["idempotency_key"] for e in db.due_for_loadbg(now, preroll_sec=5)}


def test_due_for_loadbg_excludes_loaded(tmp_path):
    """ロード済み (loaded_at) も従来どおり LOADBG 候補から外れる。"""
    db = _db(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("k2", 2, now, 1, b"")
    assert "k2" in {e["idempotency_key"] for e in db.due_for_loadbg(now, preroll_sec=5)}

    db.mark_loaded("k2")
    assert "k2" not in {e["idempotency_key"] for e in db.due_for_loadbg(now, preroll_sec=5)}


# ---- current_main_event: casparcg 再接続時の現行イベント特定 (#7) ----

_ASSET, _CM, _CUT_LIVE, _FILLER = 1, 2, 4, 5
_MAIN_ACTIONS = (_ASSET, _CUT_LIVE, _FILLER)


def test_current_main_event_picks_latest_executed(tmp_path):
    """executed 済みの本線 action のうち executed_at が最新の 1 件を返す。"""
    db = _db(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("a1", 1, base, _ASSET, b"pa1")
    db.upsert_event("a2", 2, base + timedelta(minutes=30), _ASSET, b"pa2")
    db.mark_executed("a1", base)
    db.mark_executed("a2", base + timedelta(minutes=30))

    cur = db.current_main_event(_MAIN_ACTIONS)
    assert cur is not None
    assert cur["idempotency_key"] == "a2"
    assert cur["payload"] == b"pa2"


def test_current_main_event_excludes_non_main_actions(tmp_path):
    """CM 等 actions に無い種別は、より新しくても現行に選ばない。"""
    db = _db(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("asset", 1, base, _ASSET, b"pa")
    db.upsert_event("cm", 2, base + timedelta(minutes=1), _CM, b"cm")
    db.mark_executed("asset", base)
    db.mark_executed("cm", base + timedelta(minutes=1))  # より新しいが CM

    cur = db.current_main_event(_MAIN_ACTIONS)
    assert cur is not None and cur["idempotency_key"] == "asset"


def test_current_main_event_ignores_unexecuted(tmp_path):
    """予定済みでも未 executed のイベントは現行ではない。"""
    db = _db(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("pending", 1, base, _FILLER, b"f")
    assert db.current_main_event(_MAIN_ACTIONS) is None

    db.mark_executed("pending", base)
    assert db.current_main_event(_MAIN_ACTIONS)["idempotency_key"] == "pending"


def test_current_main_event_empty_actions_is_none(tmp_path):
    db = _db(tmp_path)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("a", 1, base, _ASSET, b"x")
    db.mark_executed("a", base)
    assert db.current_main_event(()) is None


# ---- recently_received: just-in-time filler の prefetch 自己修復 (#7) ----


def test_recently_received_includes_executed(tmp_path):
    """just-in-time filler は受信即 executed され prefetch_candidates から漏れるが、
    recently_received は実行済みでも返す → 反復する次回 filler のため遅れて prefetch する。"""
    db = _db(tmp_path)
    sched = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db.upsert_event("f1", 1, sched, _FILLER, b"pf1")
    db.mark_executed("f1", sched)  # 受信即 LOADBG 失敗(404) でも executed_at が入る
    # 未来の未実行候補からは外れる
    assert "f1" not in {
        e["idempotency_key"] for e in db.prefetch_candidates(sched + timedelta(hours=4))
    }
    # recently_received には残る (received_at は upsert 時の実時刻なので直近)
    got = db.recently_received(3600)
    assert "f1" in {e["idempotency_key"] for e in got}
    assert got[0]["payload"] == b"pf1"  # prefetch ループが parse できる payload を返す


def test_recently_received_drops_old(tmp_path):
    """lookback より前に受信した event は出ない (lookback=0 は直近受信も含めない)。"""
    db = _db(tmp_path)
    db.upsert_event("old", 1, datetime(2026, 1, 1, 12, 0, tzinfo=UTC), _FILLER, b"p")
    assert db.recently_received(0) == []
