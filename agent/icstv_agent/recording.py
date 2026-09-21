# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""生放送録画 (record_live) の on/off + R2 アップロード連携。

CUT_LIVE (record=1) で MediaMTX に録画を開始させ、区間終了 (PLAY_ASSET/PLAY_FILLER) で
録画を止めて完成ファイルを RequestRecordingUpload で得た presigned PUT URL へアップロードする。
scheduling.live_recording が R2 の `ingest/live_recording/<channel_slug>/<program_id>.mp4` を
後日 scan して取り込む前提 (server 側は phase 1 で実装済み)。

最重要制約: どの処理も本線送出 (CasparCG への AMCP 発行) を遅延・失敗させてはならない。
呼び出し元 (feed_monitor) は background task として起動し、ここでの例外は全て握り潰す
(best-effort)。ブロッキング I/O (アップロード PUT / ファイル探索) は asyncio.to_thread 等で
イベントループを塞がない。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

UploadFn = Callable[[str, str], Awaitable[bool]]


def has_free_space(path: Path, min_free_bytes: int) -> bool:
    """path (またはその親で存在する最初のディレクトリ) の空き容量が閾値以上か。

    送出ノードはディスクに余裕が無いため、録画開始前の簡易ガード。判定不能 (path 未作成等) は
    安全側 (録画スキップ) に倒すため False を返す。
    """
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return False
    return usage.free >= min_free_bytes


def recording_path(recording_dir: Path, channel_slug: str, program_id: int, now) -> Path:
    """録画先ローカルパス。%Y-%m-%d_%H-%M-%S 命名 (MediaMTX recordPath と同じ規約)。"""
    fname = now.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
    return recording_dir / channel_slug / f"program-{program_id}" / fname


class RecordingManager:
    """録画 on/off + アップロードの orchestration (I/O は注入して単体テスト可能に)。"""

    def __init__(
        self,
        *,
        mediamtx,
        server_client,
        queue,
        recording_dir: Path,
        min_free_bytes: int,
        upload: UploadFn | None = None,
    ) -> None:
        self._mediamtx = mediamtx
        self._server_client = server_client
        self._queue = queue
        self._recording_dir = recording_dir
        self._min_free_bytes = min_free_bytes
        self._upload = upload or _http_put
        # path -> (channel_slug, program_id, file_path) : 現在進行中の録画。
        self._active: dict[str, tuple[str, int, str]] = {}

    async def start(self, path: str, channel_slug: str, program_id: int) -> None:
        """CUT_LIVE (record=1) 検知時に呼ぶ。失敗しても例外は投げない (best-effort)。"""
        dest = recording_path(self._recording_dir, channel_slug, program_id, datetime.now(UTC))
        if not has_free_space(self._recording_dir, self._min_free_bytes):
            logger.warning(
                "録画スキップ (ディスク空き容量不足): channel=%s program_id=%s dir=%s",
                channel_slug,
                program_id,
                self._recording_dir,
            )
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = await self._mediamtx.set_record(path, True, str(dest))
        if ok:
            self._active[path] = (channel_slug, program_id, str(dest))
            logger.info(
                "録画開始: path=%s channel=%s program_id=%s dest=%s",
                path,
                channel_slug,
                program_id,
                dest,
            )
        else:
            logger.warning(
                "録画開始失敗 (best-effort、送出は継続): path=%s channel=%s program_id=%s",
                path,
                channel_slug,
                program_id,
            )

    async def stop_and_upload(self, path: str) -> None:
        """生区間終了 (PLAY_ASSET/PLAY_FILLER) 検知時に呼ぶ。録画中でなければ no-op。"""
        entry = self._active.pop(path, None)
        if entry is None:
            return
        channel_slug, program_id, file_path = entry
        await self._mediamtx.set_record(path, False, file_path)
        logger.info("録画終了: path=%s channel=%s program_id=%s", path, channel_slug, program_id)
        await self._upload_or_queue(file_path, channel_slug, program_id)

    async def _upload_or_queue(self, file_path: str, channel_slug: str, program_id: int) -> None:
        """アップロードを試み、失敗したら outbox に積んで次回リトライへ回す (best-effort)。"""
        try:
            uploaded = await self._try_upload(file_path, channel_slug, program_id)
        except Exception:
            logger.warning("録画アップロード中に例外 (outbox へ退避): %s", file_path, exc_info=True)
            uploaded = False
        if uploaded:
            with contextlib.suppress(OSError):
                os.remove(file_path)
        else:
            self._queue.enqueue_recording_upload(file_path, channel_slug, program_id)

    async def _try_upload(self, file_path: str, channel_slug: str, program_id: int) -> bool:
        if not os.path.exists(file_path):
            logger.warning("録画ファイルが見つからない (アップロード不可): %s", file_path)
            return False
        result = await self._server_client.request_recording_upload(
            channel_slug=channel_slug, program_id=program_id
        )
        if result is None:
            logger.warning(
                "RequestRecordingUpload 失敗 (channel/program 不明): channel=%s program_id=%s",
                channel_slug,
                program_id,
            )
            return False
        upload_url, _r2_key = result
        return await self._upload(upload_url, file_path)

    async def retry_pending_uploads(self, limit: int = 5) -> None:
        """outbox に溜まった未アップロード録画を再送する (起動時/周期リトライ)。"""
        for entry in self._queue.recording_upload_outbox_iter(limit=limit):
            file_path = entry["file_path"]
            try:
                uploaded = await self._try_upload(
                    file_path, entry["channel_slug"], entry["program_id"]
                )
            except Exception:
                logger.warning("録画再アップロード中に例外: %s", file_path, exc_info=True)
                uploaded = False
            if uploaded:
                self._queue.recording_upload_outbox_delete(file_path)
                with contextlib.suppress(OSError):
                    os.remove(file_path)
            else:
                self._queue.recording_upload_outbox_bump(file_path)


async def _http_put(upload_url: str, file_path: str) -> bool:
    """presigned PUT URL へファイルをアップロード。失敗時は False (例外を投げない)。

    録画ファイルは数百MB〜数GBになりうるため、ブロッキング I/O (ファイル読み込み + PUT 送信)
    はまとめて asyncio.to_thread に逃がし、イベントループ (dispatch loop の 100ms tick 等) を
    塞がないようにする。
    """
    try:
        await asyncio.to_thread(_http_put_sync, upload_url, file_path)
        return True
    except Exception:
        logger.warning("録画 PUT アップロード失敗: %s", file_path, exc_info=True)
        return False


def _http_put_sync(upload_url: str, file_path: str) -> None:
    import httpx

    with (
        httpx.Client(timeout=httpx.Timeout(60.0, read=600.0)) as http,
        open(file_path, "rb") as f,
    ):
        resp = http.put(upload_url, content=f, headers={"Content-Type": "video/mp4"})
        resp.raise_for_status()
