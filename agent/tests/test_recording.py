# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RecordingManager: 生放送録画 on/off + アップロードの検証 (実 MediaMTX/R2/gRPC 不要)。

I/O (mediamtx.set_record / server_client.request_recording_upload / upload) を fake で注入し、
start/stop_and_upload/retry_pending_uploads の挙動を確認する。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from icstv_agent.queue_db import QueueDb
from icstv_agent.recording import RecordingManager, has_free_space


class FakeMtx:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, bool, str]] = []

    async def set_record(self, path: str, enabled: bool, record_path: str) -> bool:
        self.calls.append((path, enabled, record_path))
        return self.ok


class FakeServerClient:
    def __init__(self, upload_url: str | None = "https://r2.example/put") -> None:
        self.upload_url = upload_url
        self.requests: list[tuple[str, int]] = []

    async def request_recording_upload(self, *, channel_slug: str, program_id: int):
        self.requests.append((channel_slug, program_id))
        if self.upload_url is None:
            return None
        return self.upload_url, f"ingest/live_recording/{channel_slug}/{program_id}.mp4"


def _mk(tmp_path, *, mtx_ok=True, upload_ok=True, upload_url="https://r2.example/put"):
    queue = QueueDb(tmp_path / "queue.db")
    mtx = FakeMtx(ok=mtx_ok)
    client = FakeServerClient(upload_url=upload_url)
    uploaded: list[str] = []

    async def upload(url: str, file_path: str) -> bool:
        uploaded.append(file_path)
        return upload_ok

    mgr = RecordingManager(
        mediamtx=mtx,
        server_client=client,
        queue=queue,
        recording_dir=tmp_path / "recordings",
        min_free_bytes=0,  # テストでは容量ガードを無効化
        upload=upload,
    )
    return mgr, mtx, client, queue, uploaded


def test_has_free_space_true_when_ample(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    assert has_free_space(d, 1) is True


def test_has_free_space_false_when_over_threshold(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    # 現実的にありえない巨大閾値 → 必ず不足判定
    assert has_free_space(d, 10**18) is False


def test_has_free_space_walks_up_to_existing_parent(tmp_path):
    # まだ存在しないディレクトリでも親を辿って判定できる (mkdir 前の呼び出しを許容)。
    missing = tmp_path / "not" / "yet" / "created"
    assert has_free_space(missing, 1) is True


def test_start_calls_set_record_true_and_marks_active(tmp_path):
    mgr, mtx, _client, _queue, _uploaded = _mk(tmp_path)
    asyncio.run(mgr.start("live/ch1", "ch1", 42))
    assert len(mtx.calls) == 1
    path, enabled, record_path = mtx.calls[0]
    assert path == "live/ch1" and enabled is True
    assert "ch1" in record_path and "program-42" in record_path


def test_start_skips_when_disk_low(tmp_path):
    queue = QueueDb(tmp_path / "queue.db")
    mtx = FakeMtx()
    client = FakeServerClient()
    mgr = RecordingManager(
        mediamtx=mtx,
        server_client=client,
        queue=queue,
        recording_dir=tmp_path / "recordings",
        min_free_bytes=10**18,  # 常に不足
    )
    asyncio.run(mgr.start("live/ch1", "ch1", 42))
    assert mtx.calls == []  # ディスク不足でガードされ MediaMTX すら呼ばない


def test_stop_and_upload_success_removes_local_file(tmp_path):
    mgr, mtx, client, _queue, uploaded = _mk(tmp_path)
    asyncio.run(mgr.start("live/ch1", "ch1", 42))
    dest = mtx.calls[0][2]
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"fake mp4 bytes")

    asyncio.run(mgr.stop_and_upload("live/ch1"))

    assert mtx.calls[1] == ("live/ch1", False, dest)
    assert client.requests == [("ch1", 42)]
    assert uploaded == [dest]
    assert not Path(dest).exists()  # アップロード成功でローカル削除


def test_stop_and_upload_failure_enqueues_outbox(tmp_path):
    mgr, mtx, _client, queue, _uploaded = _mk(tmp_path, upload_ok=False)
    asyncio.run(mgr.start("live/ch1", "ch1", 42))
    dest = mtx.calls[0][2]
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"fake mp4 bytes")

    asyncio.run(mgr.stop_and_upload("live/ch1"))

    assert Path(dest).exists()  # 失敗時はローカルに残す
    pending = queue.recording_upload_outbox_iter()
    assert len(pending) == 1
    assert pending[0]["file_path"] == dest
    assert pending[0]["channel_slug"] == "ch1"
    assert pending[0]["program_id"] == 42


def test_stop_and_upload_noop_when_not_recording(tmp_path):
    mgr, mtx, client, _queue, _uploaded = _mk(tmp_path)
    asyncio.run(mgr.stop_and_upload("live/ch1"))  # start() を呼んでいない
    assert mtx.calls == []
    assert client.requests == []


def test_retry_pending_uploads_clears_outbox_on_success(tmp_path):
    mgr, _mtx, client, queue, uploaded = _mk(tmp_path)
    f = tmp_path / "orphan.mp4"
    f.write_bytes(b"data")
    queue.enqueue_recording_upload(str(f), "ch1", 7)

    asyncio.run(mgr.retry_pending_uploads())

    assert queue.recording_upload_outbox_iter() == []
    assert client.requests == [("ch1", 7)]
    assert uploaded == [str(f)]
    assert not f.exists()


def test_retry_pending_uploads_bumps_attempts_on_failure(tmp_path):
    mgr, _mtx, _client, queue, _uploaded = _mk(tmp_path, upload_ok=False)
    f = tmp_path / "orphan.mp4"
    f.write_bytes(b"data")
    queue.enqueue_recording_upload(str(f), "ch1", 7)

    asyncio.run(mgr.retry_pending_uploads())

    pending = queue.recording_upload_outbox_iter()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert f.exists()  # 失敗時はファイルを消さない


def test_upload_never_raises_on_missing_file(tmp_path):
    """録画ファイルが既に消えている (二重処理等) 場合も例外を投げず False 扱い。"""
    mgr, _mtx, client, queue, _uploaded = _mk(tmp_path)
    queue.enqueue_recording_upload(str(tmp_path / "gone.mp4"), "ch1", 1)
    asyncio.run(mgr.retry_pending_uploads())
    assert client.requests == []  # ファイル不在チェックで request すら飛ばさない
    pending = queue.recording_upload_outbox_iter()
    assert pending[0]["attempts"] == 1
