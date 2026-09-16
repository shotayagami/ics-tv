# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開番組表 (視聴者向け) の HTTP smoke。認証不要。"""

from __future__ import annotations

from datetime import timedelta


def test_public_epg_returns_200(http_client, channel):
    res = http_client.get(f"/public/ch/{channel.slug}/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert channel.name in body


def test_public_epg_unknown_channel_404(http_client, db):
    res = http_client.get("/public/ch/nonexistent/")
    assert res.status_code == 404


def test_public_epg_disabled_channel_404(http_client, db):
    from core.models import Channel

    Channel.objects.create(name="off", slug="off-ch", enabled=False)
    res = http_client.get("/public/ch/off-ch/")
    assert res.status_code == 404


def test_public_epg_shows_youtube_embed_when_live(http_client, channel):
    from django.utils import timezone

    from youtube.models import YoutubeSlot, YtSlotStatus

    now = timezone.now()
    YoutubeSlot.objects.create(
        channel=channel,
        window_start=now - timedelta(minutes=30),
        window_end=now + timedelta(minutes=90),
        status=YtSlotStatus.LIVE,
        broadcast_id="YT-LIVE-12345",
        title="now playing",
    )
    # #Phase1: 埋め込みは島が API の youtube_broadcast_id から描画する。
    data = http_client.get(f"/api/v1/channels/{channel.slug}").json()
    assert data["youtube_broadcast_id"] == "YT-LIVE-12345"


def test_public_epg_no_slot_shows_placeholder(http_client, channel):
    # 再生ソース無し → 島は「準備中」を出す (API は hls/youtube とも None)。
    data = http_client.get(f"/api/v1/channels/{channel.slug}").json()
    assert data["hls_url"] is None and data["youtube_broadcast_id"] is None


def test_public_epg_lists_visible_programs(http_client, channel, asset_ready):
    """プレイヤー画面の「本日の番組」= 現在 + これから (デザイン刷新)。非公開/過去は出さない。"""
    from django.utils import timezone

    from scheduling.models import Program, ProgramType

    now = timezone.now()
    Program.objects.create(  # 現在放送中 (本日の番組 + 見出しに出る)
        channel=channel,
        type=ProgramType.RECORDED,
        title="公開される番組",
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        asset=asset_ready,
        public_visible=True,
    )
    Program.objects.create(  # これから・非公開 (public_visible=False は出さない)
        channel=channel,
        type=ProgramType.RECORDED,
        title="非公開の番組",
        start_at=now + timedelta(hours=2),
        end_at=now + timedelta(hours=3),
        asset=asset_ready,
        public_visible=False,
    )
    res = http_client.get(f"/public/ch/{channel.slug}/")
    body = res.content.decode("utf-8")
    assert "公開される番組" in body
    assert "非公開の番組" not in body


def test_public_epg_cf_hls_player_preferred(http_client, channel):
    from django.utils import timezone

    from youtube.models import YoutubeSlot, YtSlotStatus

    channel.cf_playback_hls_url = "https://customer-x.cloudflarestream.com/uid/manifest/video.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    now = timezone.now()
    YoutubeSlot.objects.create(
        channel=channel,
        window_start=now - timedelta(minutes=30),
        window_end=now + timedelta(minutes=90),
        status=YtSlotStatus.LIVE,
        broadcast_id="YT-LIVE-9",
        title="live",
    )
    # API は両方返し、島(VideoPlayer)が hls を優先する (CF 優先は client ロジック)。
    data = http_client.get(f"/api/v1/channels/{channel.slug}").json()
    assert data["hls_url"].startswith(
        "https://customer-x.cloudflarestream.com/uid/manifest/video.m3u8?token="
    )


def test_public_epg_hls_url_drives_quality_ui(http_client, channel):
    """#Phase1: 画質切替/音声のみ UI は島が HLS levels から派生する。API は hls_url を渡すだけ。"""
    channel.cf_playback_hls_url = "https://tv.yagamin.net/hls2/ch1/master.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    data = http_client.get(f"/api/v1/channels/{channel.slug}").json()
    assert data["hls_url"].startswith("https://tv.yagamin.net/hls2/ch1/master.m3u8?token=")


def test_public_epg_no_player_source_without_hls_or_slot(http_client, channel):
    """#Phase1: 再生ソースが無ければ島はプレイヤーを出さない (API は hls/youtube とも None)。"""
    data = http_client.get(f"/api/v1/channels/{channel.slug}").json()
    assert data["hls_url"] is None and data["youtube_broadcast_id"] is None


# 注: 旧プレイヤー画面の番組サムネ表示テストは削除。デザイン刷新で公開フロントは
# 静止サムネではなく送出中のライブ映像/疑似プレビューを主役にするため (design_handoff §アセット)。
