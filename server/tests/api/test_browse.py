# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ジャンル別ブラウズ (#DISC-01 / #Phase2c): 実在ジャンルのチップ・絞り込み・シリーズ解決・公開ゲート。

グリッドは React 島が /api/v1/browse から描画するため、API ベースで検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from scheduling.models import Genre, Program, ProgramType, Series

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _program(
    channel,
    asset,
    *,
    title="番組",
    genre="",
    series=None,
    public=True,
    start_delta=timedelta(days=1),
):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        genre=genre,
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
        public_visible=public,
        series=series,
    )


@_PUBLIC_HOST
def test_browse_shows_only_present_genre_chips(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, title="深夜アニメ", genre=Genre.ANIME)
    d = http_client.get("/api/v1/browse").json()
    assert Genre.ANIME in d["genres"]  # 付与済みジャンルはチップに出る
    assert "映画" not in d["genres"]  # 付与の無いジャンルは出さない (死んだチップを作らない)


@_PUBLIC_HOST
def test_browse_filters_by_genre(http_client, channel, asset_ready, db):
    a = _program(channel, asset_ready, title="深夜アニメ", genre=Genre.ANIME)
    n = _program(
        channel, asset_ready, title="朝の情報番組", genre=Genre.NEWS, start_delta=timedelta(days=2)
    )
    ids = [p["id"] for p in http_client.get("/api/v1/browse?genre=アニメ").json()["programs"]]
    assert a.id in ids and n.id not in ids


@_PUBLIC_HOST
def test_browse_resolves_series_genre(http_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="連続もの", genre=Genre.SCIENCE)
    p = _program(
        channel, asset_ready, title="第5回", genre="", series=s
    )  # 番組自身はジャンル未設定
    ids = [x["id"] for x in http_client.get("/api/v1/browse?genre=科学").json()["programs"]]
    assert p.id in ids  # シリーズのジャンルで拾う


@_PUBLIC_HOST
def test_browse_excludes_non_public(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, title="非公開アニメ", genre=Genre.ANIME, public=False)
    ids = [x["id"] for x in http_client.get("/api/v1/browse?genre=アニメ").json()["programs"]]
    assert p.id not in ids


@_PUBLIC_HOST
def test_browse_no_genre_returns_chips_only(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, title="深夜アニメ", genre=Genre.ANIME)
    d = http_client.get("/api/v1/browse").json()
    # ジャンル未選択は結果空 (島が「上のジャンルを選んでください」を表示)
    assert d["genre"] == "" and d["programs"] == []


@_PUBLIC_HOST
def test_browse_unknown_genre_is_safe(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, title="深夜アニメ", genre=Genre.ANIME)
    res = http_client.get("/api/v1/browse?genre=存在しないジャンル")
    assert res.status_code == 200  # 不正ジャンルでも壊れない
    d = res.json()
    assert d["genre"] == "" and d["programs"] == []  # 正規化されて空


@_PUBLIC_HOST
def test_browse_series_card_uses_nearest_upcoming_broadcast(http_client, channel, asset_ready, db):
    # 週次展開で同一シリーズに複数の今後の放送回がある場合、代表カードは
    # 一番近い(次回の)放送日時を示すべき。一番先(最終展開分)ではない。
    s = Series.objects.create(channel=channel, title="週刊もの", genre=Genre.ANIME)
    nearest = _program(channel, asset_ready, title="第1回", series=s, start_delta=timedelta(days=1))
    _program(channel, asset_ready, title="第2回", series=s, start_delta=timedelta(weeks=2))
    _program(channel, asset_ready, title="第3回", series=s, start_delta=timedelta(weeks=4))
    programs = http_client.get("/api/v1/browse?genre=アニメ").json()["programs"]
    assert len(programs) == 1  # series は1枚に集約
    assert programs[0]["id"] == nearest.id


@_PUBLIC_HOST
def test_browse_series_card_falls_back_to_most_recent_past_broadcast(
    http_client, channel, asset_ready, db
):
    # 今後の放送が無いシリーズは、一番近い過去(最新の既放送回)を代表にする。
    s = Series.objects.create(channel=channel, title="終了済み", genre=Genre.ANIME)
    _program(channel, asset_ready, title="第1回", series=s, start_delta=-timedelta(weeks=4))
    most_recent_past = _program(
        channel, asset_ready, title="第2回", series=s, start_delta=-timedelta(weeks=1)
    )
    programs = http_client.get("/api/v1/browse?genre=アニメ").json()["programs"]
    assert len(programs) == 1
    assert programs[0]["id"] == most_recent_past.id


@_PUBLIC_HOST
def test_browse_series_card_prefers_upcoming_over_past(http_client, channel, asset_ready, db):
    # 過去回・未来回が混在する場合は未来回を優先する。
    s = Series.objects.create(channel=channel, title="継続中", genre=Genre.ANIME)
    _program(channel, asset_ready, title="既放送", series=s, start_delta=-timedelta(days=1))
    upcoming = _program(channel, asset_ready, title="次回", series=s, start_delta=timedelta(days=3))
    programs = http_client.get("/api/v1/browse?genre=アニメ").json()["programs"]
    assert len(programs) == 1
    assert programs[0]["id"] == upcoming.id
