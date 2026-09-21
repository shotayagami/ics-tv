# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""簡易おすすめ (#PERS-03): よく見るジャンルから未視聴の見逃しを薦める。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from members.models import Member, WatchHistory
from members.recommend import recommended_vod
from scheduling.models import Genre, Program, ProgramType, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(client, email="m@example.com"):
    client.post("/members/login/", {"email": email, "password": _PW})


def _vod(channel, asset, *, title, genre, ended_ago):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        genre=genre,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=VodVisibility.PUBLIC,
    )


@_PUBLIC_HOST
def test_recommends_unwatched_in_preferred_genre(channel, asset_ready, db):
    m = _member()
    watched = _vod(
        channel, asset_ready, title="見たアニメ", genre=Genre.ANIME, ended_ago=timedelta(hours=5)
    )
    WatchHistory.objects.create(member=m, program=watched, completed=True)
    rec = _vod(
        channel,
        asset_ready,
        title="おすすめアニメ",
        genre=Genre.ANIME,
        ended_ago=timedelta(hours=2),
    )
    result = recommended_vod(m)
    assert rec in result  # 同ジャンルの未視聴を薦める
    assert watched not in result  # 視聴済みは除外


@_PUBLIC_HOST
def test_recommend_empty_without_history(channel, asset_ready, db):
    m = _member()
    _vod(channel, asset_ready, title="アニメ", genre=Genre.ANIME, ended_ago=timedelta(hours=2))
    assert recommended_vod(m) == []  # 履歴/マイリストが無ければ空 (degrade)


@_PUBLIC_HOST
def test_recommend_only_preferred_genre(channel, asset_ready, db):
    m = _member()
    watched = _vod(
        channel, asset_ready, title="見たアニメ", genre=Genre.ANIME, ended_ago=timedelta(hours=6)
    )
    WatchHistory.objects.create(member=m, program=watched, completed=True)
    other = _vod(
        channel,
        asset_ready,
        title="別ジャンルの料理番組",
        genre=Genre.INFO,
        ended_ago=timedelta(hours=2),
    )
    result = recommended_vod(m)
    assert other not in result  # 好みでないジャンルは薦めない


@_PUBLIC_HOST
def test_home_shows_recommendations_for_member(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    watched = _vod(
        channel, asset_ready, title="見たアニメ", genre=Genre.ANIME, ended_ago=timedelta(hours=6)
    )
    WatchHistory.objects.create(member=m, program=watched, completed=True)
    rec = _vod(
        channel, asset_ready, title="続きのアニメ", genre=Genre.ANIME, ended_ago=timedelta(hours=2)
    )
    body = http_client.get("/").content.decode("utf-8")
    assert "あなたへのおすすめ" in body
    assert f"/vod/{rec.id}/" in body and "続きのアニメ" in body
