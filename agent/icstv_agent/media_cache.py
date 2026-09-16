# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""prefetch 媒体キャッシュ (overview 3.2)。

R2 の mezzanine を CasparCG のローカル媒体フォルダへ先読みし、容量超過分を LRU で退避する。
LOADBG は clip 名 (例 "asset/42") を媒体フォルダ相対で解決するため、ダウンロード先は
media_dir/<clip><ext> とする。download は注入式 (テストで差し替え可能、本番は httpx ストリーム)。
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

Downloader = Callable[[str, Path], Awaitable[None]]


class MediaCache:
    def __init__(self, media_dir: Path, max_bytes: int, download: Downloader) -> None:
        self.media_dir = Path(media_dir)
        self.max_bytes = max_bytes
        self._download = download
        # pin した path は LRU 退避の対象外 (常駐)。channel の standing メディア
        # = filler playlist 全件 / slate を確実に手元へ残し、巡回先の cache-miss(404) を防ぐ。
        self._pinned: set[Path] = set()

    def _ext(self, url: str) -> str:
        return Path(urlparse(url).path).suffix or ".mp4"

    def path_for(self, clip: str, url: str) -> Path:
        return self.media_dir / (clip + self._ext(url))

    async def ensure(self, clip: str, url: str) -> Path | None:
        """clip がローカルに無ければ url から取得。既存なら mtime を更新 (LRU の最近使用印)。"""
        path = self.path_for(clip, url)
        if path.exists():
            with contextlib.suppress(OSError):
                os.utime(path, None)  # LRU: 直近アクセスを記録
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        try:
            await self._download(url, tmp)
            os.replace(tmp, path)  # 原子的に確定 (部分ファイルを LOADBG させない)
        except Exception as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            # 例外型/内容を必ず残す。汎用文言だけだと httpx 欠落 (ModuleNotFoundError) や
            # HTTP status / TLS / DNS の切り分けができず prefetch 不達の原因が不可視になる。
            logger.warning("prefetch 失敗 clip=%s: %r", clip, e)
            return None
        self._evict()
        return path

    def pin(self, path: Path) -> None:
        """path を LRU 退避の対象外にする (standing メディア: filler セット / slate)。"""
        self._pinned.add(path)

    async def ensure_pinned(self, clip: str, url: str) -> Path | None:
        """ensure して成功したら pin する (常駐させる standing メディア用)。"""
        path = await self.ensure(clip, url)
        if path is not None:
            self.pin(path)
        return path

    def _evict(self) -> None:
        """総容量が上限超なら mtime 昇順 (古い順) に削除する。"""
        files: list[tuple[Path, int, float]] = []
        for f in self.media_dir.rglob("*"):
            if not f.is_file() or f.name.endswith(".part"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((f, st.st_size, st.st_mtime))
        total = sum(size for _, size, _ in files)
        if total <= self.max_bytes:
            return
        for f, size, _ in sorted(files, key=lambda x: x[2]):
            if f in self._pinned:
                continue  # 常駐メディア (filler セット/slate) は退避しない
            with contextlib.suppress(OSError):
                f.unlink()
                total -= size
                logger.info("prefetch LRU 退避: %s (%d bytes)", f, size)
            if total <= self.max_bytes:
                break
