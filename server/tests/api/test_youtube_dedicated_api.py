# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#23 番組専用枠の YouTube API ラッパー単体テスト。

yt() を MagicMock に差し替え、insert_broadcast の body 構築 / apply_broadcast_preset の
videos.update / create_dedicated_stream の保存を、実 API に到達せず検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from youtube import api
from youtube.models import YoutubeBroadcastPreset

pytestmark = pytest.mark.django_db


def _fake_yt(monkeypatch) -> MagicMock:
    yc = MagicMock()
    monkeypatch.setattr("youtube.api.yt", lambda channel: yc)
    return yc


def test_insert_broadcast_defaults_preserve_rolling_behavior(channel, monkeypatch):
    yc = _fake_yt(monkeypatch)
    lb = yc.liveBroadcasts.return_value
    lb.insert.return_value.execute.return_value = {"id": "BC1"}

    bid = api.insert_broadcast(
        channel,
        title="t",
        scheduled_start=timezone.now(),
        privacy="public",
        enable_monitor=True,
    )
    assert bid == "BC1"
    body = lb.insert.call_args.kwargs["body"]
    cd = body["contentDetails"]
    # 従来挙動: DVR/Embed=true・autoStart/Stop=false・made-for-kids=false・追加項目は未指定
    assert cd["enableDvr"] is True and cd["enableEmbed"] is True
    assert cd["enableAutoStart"] is False and cd["enableAutoStop"] is False
    assert cd["monitorStream"]["enableMonitorStream"] is True
    assert "latencyPreference" not in cd
    assert "recordFromStart" not in cd
    assert "enableClosedCaptions" not in cd
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert "scheduledEndTime" not in body["snippet"]
    # 既定は rolling 用 livestream に bind
    assert lb.bind.call_args.kwargs["streamId"] == channel.youtube_livestream_id


def test_insert_broadcast_preset_args_and_stream_id(channel, monkeypatch):
    yc = _fake_yt(monkeypatch)
    lb = yc.liveBroadcasts.return_value
    lb.insert.return_value.execute.return_value = {"id": "BC2"}

    end = timezone.now()
    api.insert_broadcast(
        channel,
        title="t",
        scheduled_start=timezone.now(),
        privacy="unlisted",
        enable_monitor=False,
        made_for_kids=True,
        latency_preference="ultraLow",
        enable_closed_captions=True,
        record_from_start=True,
        scheduled_end=end,
        stream_id="LS2",
    )
    body = lb.insert.call_args.kwargs["body"]
    cd = body["contentDetails"]
    assert cd["latencyPreference"] == "ultraLow"
    assert cd["enableClosedCaptions"] is True
    assert cd["closedCaptionsType"] == "closedCaptionsHttpPost"
    assert cd["recordFromStart"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is True
    assert body["snippet"]["scheduledEndTime"] == end.isoformat()
    # 専用 livestream に bind
    assert lb.bind.call_args.kwargs["streamId"] == "LS2"


def test_apply_broadcast_preset_snippet_and_status(channel, monkeypatch):
    yc = _fake_yt(monkeypatch)
    vids = yc.videos.return_value
    vids.update.return_value.execute.return_value = {}

    preset = YoutubeBroadcastPreset(
        name="p",
        category_id=24,
        tags=["VRChat", "実況"],
        privacy="public",
        license="youtube",
        enable_embed=True,
        public_stats_viewable=True,
        made_for_kids=False,
        default_language="ja",
    )
    api.apply_broadcast_preset(channel, "BC1", preset, title="My Title")

    kw = vids.update.call_args.kwargs
    assert kw["part"] == "snippet,status"
    body = kw["body"]
    assert body["snippet"]["categoryId"] == "24"  # 文字列化
    assert body["snippet"]["tags"] == ["VRChat", "実況"]
    assert body["snippet"]["defaultLanguage"] == "ja"
    assert body["status"]["license"] == "youtube"
    assert body["status"]["privacyStatus"] == "public"
    assert body["status"]["embeddable"] is True


def test_apply_broadcast_preset_status_only_when_no_category(channel, monkeypatch):
    yc = _fake_yt(monkeypatch)
    vids = yc.videos.return_value
    vids.update.return_value.execute.return_value = {}

    preset = YoutubeBroadcastPreset(
        name="p2",
        category_id=None,
        tags=[],
        privacy="unlisted",
        license="youtube",
        enable_embed=False,
        public_stats_viewable=False,
        made_for_kids=True,
    )
    api.apply_broadcast_preset(channel, "BC2", preset, title="x")
    kw = vids.update.call_args.kwargs
    assert kw["part"] == "status"  # snippet を含めない
    assert "snippet" not in kw["body"]
    assert kw["body"]["status"]["selfDeclaredMadeForKids"] is True
    # category も tags も無いので videos.list は呼ばない
    vids.list.assert_not_called()


def test_apply_broadcast_preset_adds_to_playlist(channel, monkeypatch):
    yc = _fake_yt(monkeypatch)
    yc.videos.return_value.update.return_value.execute.return_value = {}
    pl = yc.playlistItems.return_value
    pl.insert.return_value.execute.return_value = {}

    preset = YoutubeBroadcastPreset(name="p3", category_id=24, playlist_id="PL123")
    api.apply_broadcast_preset(channel, "BC3", preset, title="x")

    snippet = pl.insert.call_args.kwargs["body"]["snippet"]
    assert snippet["playlistId"] == "PL123"
    assert snippet["resourceId"]["videoId"] == "BC3"


def test_create_dedicated_stream_saves_second_stream(channel, monkeypatch):
    yc = _fake_yt(monkeypatch)
    yc.liveStreams.return_value.insert.return_value.execute.return_value = {
        "id": "LS2",
        "cdn": {
            "ingestionInfo": {
                "ingestionAddress": "rtmp://a.rtmp.youtube.com/live2",
                "streamName": "secret-key-2",  # pragma: allowlist secret - test only
            }
        },
    }
    api.create_dedicated_stream(channel)
    channel.refresh_from_db()
    assert channel.youtube_livestream_id_2 == "LS2"
    assert channel.youtube_ingest_url_2 == "rtmp://a.rtmp.youtube.com/live2"
    assert channel.youtube_stream_key_2 == "secret-key-2"  # 暗号化往復
    body = yc.liveStreams.return_value.insert.call_args.kwargs["body"]
    assert body["cdn"]["resolution"] == "variable"
    assert body["contentDetails"]["isReusable"] is True


def test_create_dedicated_stream_rejects_when_already_set(channel, monkeypatch):
    _fake_yt(monkeypatch)
    channel.youtube_livestream_id_2 = "exists"
    channel.save(update_fields=["youtube_livestream_id_2"])
    with pytest.raises(ValueError, match="already has"):
        api.create_dedicated_stream(channel)
