# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""番組詳細ページ (#EPG-01): メタdata表示・状態別CTA・出演者フォールバック・今後の放送。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from scheduling.models import Genre, Program, ProgramType, Series, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _program(
    channel,
    asset,
    *,
    start_delta,
    dur=timedelta(hours=1),
    visibility=VodVisibility.OFF,
    public=True,
    title="番組X",
    series=None,
    cast="",
    description="",
    genre="",
):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        genre=genre,
        start_at=start,
        end_at=start + dur,
        asset=asset,
        public_visible=public,
        vod_visibility=visibility,
        cast=cast,
        description=description,
        series=series,
    )


@_PUBLIC_HOST
def test_detail_renders_metadata(http_client, channel, asset_ready, db):
    p = _program(
        channel,
        asset_ready,
        start_delta=timedelta(hours=3),
        genre=Genre.NEWS,
        title="特集ニュース",
        description="今夜の特集です。",
        cast="山田 太郎、佐藤 花子",
    )
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert "特集ニュース" in body
    assert "今夜の特集です。" in body
    assert "山田 太郎" in body
    assert "ニュース" in body  # ジャンル


@_PUBLIC_HOST
def test_detail_non_public_404(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, start_delta=timedelta(hours=3), public=False)
    assert http_client.get(f"/program/{p.id}/").status_code == 404


@_PUBLIC_HOST
def test_detail_live_shows_live_cta(http_client, channel, asset_ready, db):
    # #Phase2c: 操作バー(CTA)は React 島が /api/v1/program/{id} から描画。ページは島マウントのみ。
    p = _program(channel, asset_ready, start_delta=timedelta(minutes=-10), dur=timedelta(hours=1))
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert 'id="program-actions-island"' in body
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["state"] == "live"
    assert d["cta"]["kind"] == "live" and d["cta"]["url"] == f"/ch/{channel.slug}/"


@_PUBLIC_HOST
def test_detail_aired_vod_shows_replay_cta(http_client, channel, asset_ready, db):
    p = _program(
        channel, asset_ready, start_delta=timedelta(hours=-3), visibility=VodVisibility.PUBLIC
    )
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["state"] == "aired"
    assert d["cta"]["kind"] == "vod" and d["cta"]["url"] == f"/vod/{p.id}/"


@_PUBLIC_HOST
def test_detail_aired_without_vod_has_no_replay(http_client, channel, asset_ready, db):
    p = _program(
        channel, asset_ready, start_delta=timedelta(hours=-3), visibility=VodVisibility.OFF
    )
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["cta"]["kind"] != "vod"  # 録画(VOD OFF)・非ライブ → 見逃し再生 CTA は出ない


@_PUBLIC_HOST
def test_detail_upcoming_shows_schedule(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, start_delta=timedelta(days=1))
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert "放送予定" in body


@_PUBLIC_HOST
def test_cast_falls_back_to_series(http_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="レギュラー番組", cast="司会 ＡＢＣ")
    p = _program(channel, asset_ready, start_delta=timedelta(hours=3), series=s, cast="")
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert "司会 ＡＢＣ" in body  # 番組 cast 空 → series.cast にフォールバック


@_PUBLIC_HOST
def test_detail_has_share_meta(http_client, channel, asset_ready, db):
    # COMM-02: 共有ボタンは操作バー島が描く。API は共有メタ (番組ページ URL/タイトル) を返す。
    p = _program(channel, asset_ready, start_delta=timedelta(hours=3), title="特集ニュース")
    d = http_client.get(f"/api/v1/program/{p.id}").json()
    assert d["share_title"] == "特集ニュース"
    assert d["share_url"].endswith(f"/program/{p.id}/")  # API ではなく番組ページを共有


@_PUBLIC_HOST
def test_upcoming_airings_of_series_listed(http_client, channel, asset_ready, db):
    s = Series.objects.create(channel=channel, title="連続ドラマ")
    aired = _program(channel, asset_ready, start_delta=timedelta(hours=-3), series=s, title="第1回")
    _program(channel, asset_ready, start_delta=timedelta(days=2), series=s, title="第2回")
    body = http_client.get(f"/program/{aired.id}/").content.decode("utf-8")
    assert "今後の放送" in body and "第2回" in body
