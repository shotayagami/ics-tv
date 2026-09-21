# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""検索 (#DISC-02 / #Phase2c): 番組名/あらすじ/出演者/ジャンル/シリーズ横断・チャンネル名。

結果は React 島が /api/v1/search からライブ検索で描画するため、API ベースで検証する。
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
    cast="",
    description="",
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
        cast=cast,
        description=description,
        series=series,
    )


def _titles(http_client, q):
    return [p["title"] for p in http_client.get(f"/api/v1/search?q={q}").json()["programs"]]


@_PUBLIC_HOST
def test_search_by_title(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, title="特集スポーツ中継")
    _program(channel, asset_ready, title="朝のニュース", start_delta=timedelta(days=2))
    t = _titles(http_client, "スポーツ")
    assert "特集スポーツ中継" in t and "朝のニュース" not in t


@_PUBLIC_HOST
def test_search_by_cast(http_client, channel, asset_ready, db):
    _program(channel, asset_ready, title="トーク番組", cast="ゲスト 鈴木 一郎")
    assert "トーク番組" in _titles(http_client, "鈴木")


@_PUBLIC_HOST
def test_search_by_description_and_genre(http_client, channel, asset_ready, db):
    _program(
        channel,
        asset_ready,
        title="ドキュメント",
        description="深海の生態を追う",
        genre=Genre.SCIENCE,
    )
    assert "ドキュメント" in _titles(http_client, "深海")
    assert "ドキュメント" in _titles(http_client, "科学")  # ジャンル値


@_PUBLIC_HOST
def test_search_by_series_cast_fallback(http_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="連続もの", cast="主演 田中 花子")
    _program(channel, asset_ready, title="第3回", series=s)  # 番組自身に cast なし
    assert "第3回" in _titles(http_client, "田中")


@_PUBLIC_HOST
def test_search_excludes_non_public(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, title="非公開特番", public=False)
    d = http_client.get("/api/v1/search?q=非公開特番").json()
    assert all(x["id"] != p.id for x in d["programs"])
    assert d["programs"] == []  # 一致なし


@_PUBLIC_HOST
def test_search_channel_by_name(http_client, channel, asset_ready, db):
    chans = http_client.get("/api/v1/search?q=ICS").json()["channels"]
    assert any(c["slug"] == channel.slug for c in chans)  # チャンネル名ヒット


@_PUBLIC_HOST
def test_search_empty_query(http_client, channel, db):
    d = http_client.get("/api/v1/search").json()
    assert d["q"] == "" and d["programs"] == [] and d["channels"] == []


@_PUBLIC_HOST
def test_header_search_form_is_functional(http_client, channel, db):
    # nav の検索窓は実フォーム (action=/search/, input name=q)。public_base SSR なので不変。
    body = http_client.get("/").content.decode("utf-8")
    assert 'action="/search/"' in body and 'name="q"' in body
