# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""agent ローカル SQLite キュー (WAN 断耐性 = 数十時間分保持)。

設計:
- 一次キーは idempotency_key (UUID)。同じ key の再受信は最新で上書き。
- sync_seq は単調増加。resume cursor は MAX(sync_seq)。
- tombstone を受けたら該当行を削除 (= スキップ確定後 ReportResult で SKIPPED 返送)。
- payload はシリアライズ済み PlayoutEvent (proto bytes) を BLOB で保持し、
  dispatch 時に Unmarshal して AMCP を組み立てる。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playout_event (
    idempotency_key TEXT PRIMARY KEY,
    sync_seq        INTEGER NOT NULL,
    scheduled_at    TEXT NOT NULL,         -- ISO 8601 (UTC)
    action          INTEGER NOT NULL,
    payload         BLOB NOT NULL,         -- serialized icstv.v1.PlayoutEvent
    received_at     TEXT NOT NULL,
    loaded_at       TEXT,                  -- LOADBG 完了時刻 (PREROLL 段)
    executed_at     TEXT                   -- PLAY 完了時刻 (TAKE 段)
);
CREATE INDEX IF NOT EXISTS idx_pe_sched ON playout_event(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_pe_seq   ON playout_event(sync_seq);
CREATE INDEX IF NOT EXISTS idx_pe_recv  ON playout_event(received_at);

CREATE TABLE IF NOT EXISTS as_run_outbox (
    idempotency_key TEXT PRIMARY KEY,
    payload         BLOB NOT NULL,         -- serialized ReportResultRequest
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT
);

-- feed 断退避/復帰など agent 起点の割り込み報告 (ReportInterrupt) の store-and-forward。
-- as_run_outbox とは型 (ReportInterruptRequest) が違うため別テーブル (docs/operations.md O4)。
CREATE TABLE IF NOT EXISTS interrupt_outbox (
    interrupt_key   TEXT PRIMARY KEY,
    payload         BLOB NOT NULL,         -- serialized ReportInterruptRequest
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT
);

-- agent ローカル永続状態 (再起動で維持)。例: auto_return (自動復帰トグルの operator 意図)。
CREATE TABLE IF NOT EXISTS agent_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 生放送録画 (record_live) の完成クリップを R2 へ PUT できなかった際の store-and-forward。
-- 一次キーはローカルファイルパス (1 録画 = 1 ファイル = 1 行)。アップロード成功でファイルごと削除。
CREATE TABLE IF NOT EXISTS recording_upload_outbox (
    file_path       TEXT PRIMARY KEY,
    channel_slug    TEXT NOT NULL,
    program_id      INTEGER NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT
);
"""

# 既存 DB に対する in-place 追加列。CREATE TABLE IF NOT EXISTS は既存テーブルに列を足さないので
# PRAGMA で検査して ALTER ADD する。Phase 1 は version カラム不要 (列数だけで判定)。
_REQUIRED_COLUMNS = {
    "playout_event": [("loaded_at", "TEXT"), ("executed_at", "TEXT")],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _REQUIRED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, typ in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


class QueueDb:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        _ensure_columns(self._conn)

    @contextmanager
    def _tx(self):
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            yield cur
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    def last_known_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(sync_seq), 0) FROM playout_event").fetchone()
        return int(row[0])

    def upsert_event(
        self,
        idempotency_key: str,
        sync_seq: int,
        scheduled_at: datetime,
        action: int,
        payload: bytes,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO playout_event "
                "(idempotency_key, sync_seq, scheduled_at, action, payload, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "  sync_seq=excluded.sync_seq, "
                "  scheduled_at=excluded.scheduled_at, "
                "  action=excluded.action, "
                "  payload=excluded.payload, "
                "  received_at=excluded.received_at",
                (
                    idempotency_key,
                    sync_seq,
                    scheduled_at.isoformat(),
                    action,
                    payload,
                    datetime.utcnow().isoformat(),
                ),
            )

    def remove_event(self, idempotency_key: str) -> None:
        """tombstone 受信時。"""
        with self._tx() as cur:
            cur.execute("DELETE FROM playout_event WHERE idempotency_key=?", (idempotency_key,))

    def pending_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM playout_event").fetchone()
        return int(row[0])

    def due_events(self, before: datetime, limit: int = 32) -> list[dict]:
        """scheduled_at <= before の event を取り出す (旧 API、互換維持)。

        新ロジックは due_for_loadbg / due_for_take を使う (2 段分離)。
        """
        rows = self._conn.execute(
            "SELECT idempotency_key, sync_seq, scheduled_at, action, payload "
            "FROM playout_event WHERE scheduled_at <= ? ORDER BY scheduled_at LIMIT ?",
            (before.isoformat(), limit),
        ).fetchall()
        return [
            {
                "idempotency_key": r[0],
                "sync_seq": r[1],
                "scheduled_at": datetime.fromisoformat(r[2]),
                "action": r[3],
                "payload": r[4],
            }
            for r in rows
        ]

    def prefetch_candidates(self, until: datetime, limit: int = 128) -> list[dict]:
        """先読み対象: scheduled_at <= until かつ未実行 (executed_at IS NULL) の event。

        prefetch ループが payload を parse して media_url/clip を取り出し、媒体を先読みする。
        """
        rows = self._conn.execute(
            "SELECT idempotency_key, sync_seq, scheduled_at, action, payload "
            "FROM playout_event WHERE scheduled_at <= ? AND executed_at IS NULL "
            "ORDER BY scheduled_at LIMIT ?",
            (until.isoformat(), limit),
        ).fetchall()
        return [
            {
                "idempotency_key": r[0],
                "sync_seq": r[1],
                "scheduled_at": datetime.fromisoformat(r[2]),
                "action": r[3],
                "payload": r[4],
            }
            for r in rows
        ]

    def recently_received(self, lookback_sec: int, limit: int = 128) -> list[dict]:
        """直近 lookback_sec 以内に受信した event を返す (実行済み executed も含む)。

        just-in-time に届く filler は受信即 executed され prefetch_candidates から漏れるが、
        同じ clip が反復するため、受信済みのものも prefetch 対象に含めて次回の LOADBG に
        間に合わせる (自己修復)。received_at は upsert 時の naive UTC isoformat。
        """
        from datetime import timedelta

        since = (datetime.utcnow() - timedelta(seconds=lookback_sec)).isoformat()
        rows = self._conn.execute(
            "SELECT idempotency_key, sync_seq, scheduled_at, action, payload "
            "FROM playout_event WHERE received_at >= ? "
            "ORDER BY received_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [
            {
                "idempotency_key": r[0],
                "sync_seq": r[1],
                "scheduled_at": datetime.fromisoformat(r[2]),
                "action": r[3],
                "payload": r[4],
            }
            for r in rows
        ]

    def due_for_loadbg(self, now: datetime, preroll_sec: int, limit: int = 4) -> list[dict]:
        """scheduled_at <= now + preroll かつ未ロード・未実行の event (LOADBG 候補)。

        executed_at IS NULL も条件に含める: take 段で実行(失敗含む)済みのイベントを
        loaded_at=NULL のまま LOADBG 再試行し続けない。これが無いと、実在しない clip を
        参照する孤児/失敗イベントが毎 tick 404→goto_slate を繰り返し本編を隠す (停波の根本)。
        """
        from datetime import timedelta

        threshold = now + timedelta(seconds=preroll_sec)
        rows = self._conn.execute(
            "SELECT idempotency_key, sync_seq, scheduled_at, action, payload "
            "FROM playout_event "
            "WHERE scheduled_at <= ? AND loaded_at IS NULL AND executed_at IS NULL "
            "ORDER BY scheduled_at LIMIT ?",
            (threshold.isoformat(), limit),
        ).fetchall()
        return [
            {
                "idempotency_key": r[0],
                "sync_seq": r[1],
                "scheduled_at": datetime.fromisoformat(r[2]),
                "action": r[3],
                "payload": r[4],
            }
            for r in rows
        ]

    def due_for_take(self, now: datetime, limit: int = 4) -> list[dict]:
        """scheduled_at <= now かつ executed_at IS NULL の event (PLAY 候補)。"""
        rows = self._conn.execute(
            "SELECT idempotency_key, sync_seq, scheduled_at, action, payload "
            "FROM playout_event "
            "WHERE scheduled_at <= ? AND executed_at IS NULL "
            "ORDER BY scheduled_at LIMIT ?",
            (now.isoformat(), limit),
        ).fetchall()
        return [
            {
                "idempotency_key": r[0],
                "sync_seq": r[1],
                "scheduled_at": datetime.fromisoformat(r[2]),
                "action": r[3],
                "payload": r[4],
            }
            for r in rows
        ]

    def current_main_event(self, actions: tuple[int, ...]) -> dict | None:
        """本線 layer に出ているべき直近の executed イベントを 1 件返す (再接続時の re-take 用)。

        actions に与えた本線持続 action (PLAY_ASSET / CUT_LIVE / PLAY_FILLER 等) のうち
        executed_at が最新のもの。CM/bundle/slate は本線の持続状態ではないので呼び出し側で
        actions から外す (ループさせると広告が無限再生になる等)。該当無しは None。
        proto を storage 層に持ち込まないため action コードは引数で受ける。
        """
        if not actions:
            return None
        placeholders = ",".join("?" * len(actions))
        row = self._conn.execute(
            "SELECT idempotency_key, sync_seq, scheduled_at, action, payload "
            "FROM playout_event "
            f"WHERE executed_at IS NOT NULL AND action IN ({placeholders}) "
            "ORDER BY executed_at DESC, sync_seq DESC LIMIT 1",
            actions,
        ).fetchone()
        if row is None:
            return None
        return {
            "idempotency_key": row[0],
            "sync_seq": row[1],
            "scheduled_at": datetime.fromisoformat(row[2]),
            "action": row[3],
            "payload": row[4],
        }

    def mark_loaded(self, idempotency_key: str, when: datetime | None = None) -> None:
        ts = (when or datetime.utcnow()).isoformat()
        with self._tx() as cur:
            cur.execute(
                "UPDATE playout_event SET loaded_at=? WHERE idempotency_key=?",
                (ts, idempotency_key),
            )

    def mark_executed(self, idempotency_key: str, when: datetime | None = None) -> None:
        ts = (when or datetime.utcnow()).isoformat()
        with self._tx() as cur:
            cur.execute(
                "UPDATE playout_event SET executed_at=? WHERE idempotency_key=?",
                (ts, idempotency_key),
            )

    def enqueue_outbox(self, idempotency_key: str, payload: bytes) -> None:
        """ReportResult を WAN 断時にバッファ。再送ループが拾う。"""
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO as_run_outbox "
                "(idempotency_key, payload, attempts, last_attempt_at) VALUES (?, ?, 0, NULL)",
                (idempotency_key, payload),
            )

    def outbox_iter(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT idempotency_key, payload, attempts FROM as_run_outbox "
            "ORDER BY attempts, idempotency_key LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"idempotency_key": r[0], "payload": r[1], "attempts": r[2]} for r in rows]

    def outbox_delete(self, idempotency_key: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM as_run_outbox WHERE idempotency_key=?",
                (idempotency_key,),
            )

    def outbox_bump(self, idempotency_key: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE as_run_outbox SET attempts=attempts+1, last_attempt_at=? "
                "WHERE idempotency_key=?",
                (datetime.utcnow().isoformat(), idempotency_key),
            )

    # ---- interrupt outbox (ReportInterrupt の store-and-forward) ----

    def enqueue_interrupt(self, interrupt_key: str, payload: bytes) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO interrupt_outbox "
                "(interrupt_key, payload, attempts, last_attempt_at) VALUES (?, ?, 0, NULL)",
                (interrupt_key, payload),
            )

    def interrupt_outbox_iter(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT interrupt_key, payload, attempts FROM interrupt_outbox "
            "ORDER BY attempts, interrupt_key LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"interrupt_key": r[0], "payload": r[1], "attempts": r[2]} for r in rows]

    def interrupt_outbox_delete(self, interrupt_key: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM interrupt_outbox WHERE interrupt_key=?", (interrupt_key,))

    def interrupt_outbox_bump(self, interrupt_key: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE interrupt_outbox SET attempts=attempts+1, last_attempt_at=? "
                "WHERE interrupt_key=?",
                (datetime.utcnow().isoformat(), interrupt_key),
            )

    # ---- recording upload outbox (録画クリップ PUT の store-and-forward) ----

    def enqueue_recording_upload(self, file_path: str, channel_slug: str, program_id: int) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO recording_upload_outbox "
                "(file_path, channel_slug, program_id, attempts, last_attempt_at) "
                "VALUES (?, ?, ?, 0, NULL)",
                (file_path, channel_slug, program_id),
            )

    def recording_upload_outbox_iter(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT file_path, channel_slug, program_id, attempts "
            "FROM recording_upload_outbox ORDER BY attempts, file_path LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "file_path": r[0],
                "channel_slug": r[1],
                "program_id": r[2],
                "attempts": r[3],
            }
            for r in rows
        ]

    def recording_upload_outbox_delete(self, file_path: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM recording_upload_outbox WHERE file_path=?", (file_path,))

    def recording_upload_outbox_bump(self, file_path: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE recording_upload_outbox SET attempts=attempts+1, last_attempt_at=? "
                "WHERE file_path=?",
                (datetime.utcnow().isoformat(), file_path),
            )

    # ---- agent ローカル永続状態 ----

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM agent_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO agent_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self._conn.close()
