# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""server の prefetch manifest を agent が全件 pre-cache+pin し、slate を反映することを検証 (Phase2)。

filler ローテーションの巡回先 (standing set) を確実に手元へ常駐させ、未キャッシュ→LOADBG
404→slate 固着 を根絶する土台。manifest の slate 項目は per-channel slate clip として反映する。
"""

from __future__ import annotations

import asyncio
import json

from icstv_agent import main
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.media_cache import MediaCache


def _dl(content=b"x" * 50):
    async def download(url, dest):
        dest.write_bytes(content)

    return download


def test_prefetch_manifest_caches_pins_and_sets_slate(tmp_path):
    cache = MediaCache(tmp_path, 10**9, _dl())
    cm = ChannelMedia("slate/please_wait")
    manifest = json.dumps(
        [
            {"clip": "filler/2", "url": "https://s/filler/2.mp4"},
            {"clip": "filler/3", "url": "https://s/filler/3.mp4"},
            {"clip": "slate/9", "url": "https://s/slate/9.mp4", "slate": True},
        ]
    )
    asyncio.run(main._prefetch_manifest(cache, manifest, cm))
    # 全 clip がローカルへ取得される
    assert (tmp_path / "filler/2.mp4").exists()
    assert (tmp_path / "filler/3.mp4").exists()
    assert (tmp_path / "slate/9.mp4").exists()
    # 全て pin されている (LRU 退避対象外 = 常駐)
    assert (tmp_path / "filler/2.mp4") in cache._pinned
    assert (tmp_path / "slate/9.mp4") in cache._pinned
    # slate 項目が per-channel slate clip として反映される
    assert cm.slate_clip == "slate/9"


def test_prefetch_manifest_sets_exposure_policy_fillers(tmp_path):
    """exposure_policy (#27) の site_only_filler/members_filler manifest 項目を反映する。"""
    cache = MediaCache(tmp_path, 10**9, _dl())
    cm = ChannelMedia("slate/please_wait", "filler/site_only_default", "filler/members_default")
    manifest = json.dumps(
        [
            {
                "clip": "site_only_filler/5",
                "url": "https://s/site_only_filler/5.mp4",
                "site_only_filler": True,
            },
            {
                "clip": "members_filler/6",
                "url": "https://s/members_filler/6.mp4",
                "members_filler": True,
            },
        ]
    )
    asyncio.run(main._prefetch_manifest(cache, manifest, cm))
    assert (tmp_path / "site_only_filler/5.mp4") in cache._pinned
    assert (tmp_path / "members_filler/6.mp4") in cache._pinned
    assert cm.site_only_filler_clip == "site_only_filler/5"
    assert cm.members_filler_clip == "members_filler/6"


def test_prefetch_manifest_without_exposure_fillers_falls_back_to_default(tmp_path):
    """manifest に site_only_filler/members_filler 項目が無ければノードローカル既定へ戻す。"""
    cache = MediaCache(tmp_path, 10**9, _dl())
    cm = ChannelMedia("slate/please_wait", "filler/site_only_default", "filler/members_default")
    manifest = json.dumps([{"clip": "filler/2", "url": "https://s/filler/2.mp4"}])
    asyncio.run(main._prefetch_manifest(cache, manifest, cm))
    assert cm.site_only_filler_clip == "filler/site_only_default"
    assert cm.members_filler_clip == "filler/members_default"


def test_prefetch_manifest_without_slate_falls_back_to_default(tmp_path):
    cache = MediaCache(tmp_path, 10**9, _dl())
    cm = ChannelMedia("slate/please_wait")
    manifest = json.dumps([{"clip": "filler/2", "url": "https://s/filler/2.mp4"}])
    asyncio.run(main._prefetch_manifest(cache, manifest, cm))
    # slate 項目が無ければ既定 (ノードローカル please_wait) のまま
    assert cm.slate_clip == "slate/please_wait"


def test_prefetch_manifest_bad_json_is_safe(tmp_path):
    cache = MediaCache(tmp_path, 10**9, _dl())
    cm = ChannelMedia("slate/please_wait")
    # 壊れた JSON でも例外を投げず本線に影響させない
    asyncio.run(main._prefetch_manifest(cache, "not json", cm))
    assert list(tmp_path.rglob("*")) == []
    assert cm.slate_clip == "slate/please_wait"


def test_prefetch_manifest_skips_items_missing_fields(tmp_path):
    cache = MediaCache(tmp_path, 10**9, _dl())
    cm = ChannelMedia("slate/please_wait")
    manifest = json.dumps(
        [
            {"clip": "filler/2", "url": "https://s/filler/2.mp4"},
            {"clip": "filler/3"},  # url 欠 → スキップ
            {"url": "https://s/x.mp4"},  # clip 欠 → スキップ
        ]
    )
    asyncio.run(main._prefetch_manifest(cache, manifest, cm))
    assert (tmp_path / "filler/2.mp4").exists()
    assert not (tmp_path / "filler/3.mp4").exists()
