# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""トップの発見導線 (#DISC-01): 新着見逃し + ジャンルから探す。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from scheduling.models import Genre, Program, ProgramType, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _vod_program(
    channel, asset, *, title="見逃せる番組", genre="", visibility=VodVisibility.PUBLIC, end=None
):
    end = end or (timezone.now() - timedelta(hours=2))
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        genre=genre,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=visibility,
    )


@_PUBLIC_HOST
def test_home_shows_new_vod_section(http_client, channel, asset_ready, db):
    p = _vod_program(channel, asset_ready, title="新着の見逃し回")
    body = http_client.get("/").content.decode("utf-8")
    assert "新着見逃し" in body
    assert f"/vod/{p.id}/" in body and "新着の見逃し回" in body


@_PUBLIC_HOST
def test_home_shows_genre_chips_to_browse(http_client, channel, asset_ready, db):
    _vod_program(channel, asset_ready, genre=Genre.ANIME)
    body = http_client.get("/").content.decode("utf-8")
    assert "ジャンルから探す" in body
    assert "/browse/?genre=" in body  # チップが発見ブラウズへ


@_PUBLIC_HOST
def test_home_hides_new_vod_when_empty(http_client, channel, db):
    body = http_client.get("/").content.decode("utf-8")
    assert ">新着見逃し</h2>" not in body  # 見逃せる番組が無ければセクションごと出さない


@_PUBLIC_HOST
def test_home_hides_popular_when_only_overlaps_new(http_client, channel, asset_ready, db):
    # 在庫が薄く「人気」が「新着」と同じ顔ぶれになる間は、水増しを避けて人気節を畳む (#4)。
    from analytics.models import ProgramView

    p = _vod_program(channel, asset_ready, title="話題だが新着と重複")
    ProgramView.objects.create(program=p, viewer_id="v1")
    body = http_client.get("/").content.decode("utf-8")
    assert ">新着見逃し</h2>" in body  # 新着には出る
    assert ">人気の見逃し</h2>" not in body  # 同一内容なので人気節は畳む


@_PUBLIC_HOST
def test_home_shows_popular_when_distinct_from_new(http_client, channel, asset_ready, db):
    # #DISC-01 ランキング: 人気の番組が「新着」(最新8件) の外にあれば人気節が独立して出る。
    from analytics.models import ProgramView

    now = timezone.now()
    for i in range(8):  # 最新8件 (= 新着) を埋める
        _vod_program(channel, asset_ready, title=f"新着{i}", end=now - timedelta(hours=i + 1))
    old = _vod_program(channel, asset_ready, title="殿堂入りの旧作", end=now - timedelta(days=3))
    ProgramView.objects.create(program=old, viewer_id="v1")
    body = http_client.get("/").content.decode("utf-8")
    assert ">人気の見逃し</h2>" in body and "殿堂入りの旧作" in body
