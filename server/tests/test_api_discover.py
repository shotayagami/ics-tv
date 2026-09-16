# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2c: 公開ディスカバリ API (search / browse / vod)。"""

from datetime import timedelta

from django.test import Client
from django.utils import timezone


def _program(channel, asset, *, title="番組", genre="", days_ahead=1):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        genre=genre,
        asset=asset,
        start_at=now + timedelta(days=days_ahead),
        end_at=now + timedelta(days=days_ahead, hours=1),
        public_visible=True,
    )


def test_search_empty(db):
    d = Client().get("/api/v1/search").json()
    assert d["q"] == "" and d["programs"] == [] and d["channels"] == []


def test_search_finds_program_and_channel(channel, asset_ready):
    _program(channel, asset_ready, title="検索対象アニメ特集", genre="アニメ")
    d = Client().get("/api/v1/search?q=検索対象").json()
    assert any(p["title"] == "検索対象アニメ特集" for p in d["programs"])
    row = next(p for p in d["programs"] if p["title"] == "検索対象アニメ特集")
    assert row["channel_name"] == channel.name and "(" in row["start_display"]
    # チャンネル名一致でチップに出る
    d2 = Client().get(f"/api/v1/search?q={channel.name}").json()
    assert any(c["slug"] == channel.slug for c in d2["channels"])


def test_browse_genres_and_filter(channel, asset_ready):
    _program(channel, asset_ready, title="ブラウズ音楽番組", genre="音楽")
    d = Client().get("/api/v1/browse").json()
    assert "音楽" in d["genres"]
    d2 = Client().get("/api/v1/browse?genre=音楽").json()
    assert d2["genre"] == "音楽"
    assert any(p["title"] == "ブラウズ音楽番組" for p in d2["programs"])


def test_vod_endpoint_shape(channel):
    d = Client().get("/api/v1/vod").json()
    assert "items" in d and "live_archives" in d and "featured" in d
    assert isinstance(d["items"], list)


def _aired_vod(channel, asset, *, title, featured=False):
    from scheduling.models import Program, ProgramType, VodVisibility

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=now - timedelta(hours=2),
        end_at=now - timedelta(hours=1),
        asset=asset,
        vod_visibility=VodVisibility.PUBLIC,
        public_visible=True,
        is_featured=featured,
    )


def test_vod_featured_flag_selects_hero(channel, asset_ready):
    _aired_vod(channel, asset_ready, title="特集ヒーロー番組", featured=True)
    d = Client().get("/api/v1/vod").json()
    assert d["featured"] and d["featured"]["title"] == "特集ヒーロー番組"


def test_vod_featured_none_when_unflagged(channel, asset_ready):
    _aired_vod(channel, asset_ready, title="通常見逃し", featured=False)
    d = Client().get("/api/v1/vod").json()
    assert d["featured"] is None
    assert any(i["title"] == "通常見逃し" for i in d["items"])  # 一覧には出る
