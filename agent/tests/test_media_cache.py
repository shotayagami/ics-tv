# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""prefetch MediaCache (overview 3.2): DL/既存スキップ/拡張子/LRU 退避/失敗時。"""

from __future__ import annotations

import asyncio
import os

from icstv_agent.media_cache import MediaCache


def _dl(content=b"x" * 100):
    async def download(url, dest):
        dest.write_bytes(content)

    return download


def test_ensure_downloads_missing(tmp_path):
    cache = MediaCache(tmp_path, 10**9, _dl(b"abc"))
    p = asyncio.run(cache.ensure("asset/42", "https://signed/mezzanine/asset/42.mp4?x=1"))
    assert p == tmp_path / "asset/42.mp4"
    assert p.read_bytes() == b"abc"
    assert not (tmp_path / "asset/42.mp4.part").exists()  # 部分ファイルは確定済


def test_ensure_skips_existing(tmp_path):
    calls = []

    async def download(url, dest):
        calls.append(url)
        dest.write_bytes(b"z")

    cache = MediaCache(tmp_path, 10**9, download)
    asyncio.run(cache.ensure("cm/1", "https://s/cm/1.mp4"))
    asyncio.run(cache.ensure("cm/1", "https://s/cm/1.mp4"))
    assert len(calls) == 1  # 2 回目はローカルにあるので DL しない


def test_path_ext_from_url(tmp_path):
    cache = MediaCache(tmp_path, 10**9, _dl())
    assert cache.path_for("asset/9", "https://s/x/9.mxf?a=b") == tmp_path / "asset/9.mxf"
    assert cache.path_for("asset/9", "https://s/x/9") == tmp_path / "asset/9.mp4"  # 無し→mp4


def test_lru_evicts_oldest(tmp_path):
    cache = MediaCache(tmp_path, 250, _dl(b"x" * 100))  # 上限 250B、各 100B
    asyncio.run(cache.ensure("a", "https://s/a.mp4"))
    os.utime(tmp_path / "a.mp4", (1, 1))  # a を最古に
    asyncio.run(cache.ensure("b", "https://s/b.mp4"))  # 200 <= 250
    asyncio.run(cache.ensure("c", "https://s/c.mp4"))  # 300 > 250 → 最古 a を退避
    assert not (tmp_path / "a.mp4").exists()
    assert (tmp_path / "b.mp4").exists()
    assert (tmp_path / "c.mp4").exists()


def test_pinned_not_evicted(tmp_path):
    """pin した standing メディア (filler セット/slate) は最古でも LRU 退避されない。"""
    cache = MediaCache(tmp_path, 250, _dl(b"x" * 100))  # 上限 250B、各 100B
    asyncio.run(cache.ensure_pinned("a", "https://s/a.mp4"))  # pin
    os.utime(tmp_path / "a.mp4", (1, 1))  # a を最古に (本来なら最初に退避される)
    asyncio.run(cache.ensure("b", "https://s/b.mp4"))  # 200 <= 250
    os.utime(tmp_path / "b.mp4", (2, 2))  # 非 pin の最古 = b (退避対象を決定的に)
    asyncio.run(cache.ensure("c", "https://s/c.mp4"))  # 300 > 250 → pin の a を残し b を退避
    assert (tmp_path / "a.mp4").exists()  # pinned は最古でも残る
    assert not (tmp_path / "b.mp4").exists()  # 非 pin の最古が退避される
    assert (tmp_path / "c.mp4").exists()


def test_download_failure_returns_none(tmp_path):
    async def download(url, dest):
        raise RuntimeError("boom")

    cache = MediaCache(tmp_path, 10**9, download)
    p = asyncio.run(cache.ensure("x/1", "https://s/x/1.mp4"))
    assert p is None
    assert not (tmp_path / "x/1.mp4").exists()
    assert not (tmp_path / "x/1.mp4.part").exists()  # 部分ファイルを残さない
