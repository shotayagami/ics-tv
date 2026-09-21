# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""放送休止 (broadcast_windows) 中の公開表示の回帰防止。

agent は休止時間帯も PLAY_SLATE スレートを送出中で is_online は True のままなので、
home カード/nav の「LIVE n」バッジ/channels API がこれまで online のみで判定しており
「放送中」/LIVE と誤表示していた。is_broadcast_paused で online と切り離して判定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from playout.models import AgentStatus, PlayoutAction, PlayoutEvent

pytestmark = pytest.mark.django_db


def _set_broadcast_windows(channel):
    # 06:00-10:00 / 16:00-24:00 JST 運用の想定。
    channel.broadcast_windows = [
        {"start": "06:00", "end": "10:00"},
        {"start": "16:00", "end": "24:00"},
    ]
    channel.save(update_fields=["broadcast_windows"])


@pytest.fixture
def fixed_off_air_now(monkeypatch):
    """2026-07-02 13:38 JST 相当 (上記 windows のどちらにも当たらない off-air 時刻)。"""
    fixed_now = datetime(2026, 7, 2, 4, 38, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: fixed_now)
    return fixed_now


def test_is_broadcast_paused_true_outside_window(channel):
    from core.now_playing import is_broadcast_paused

    _set_broadcast_windows(channel)
    now = datetime(2026, 7, 2, 4, 38, tzinfo=UTC)  # = 13:38 JST
    assert is_broadcast_paused(channel, now) is True


def test_is_broadcast_paused_false_inside_window(channel):
    from core.now_playing import is_broadcast_paused

    _set_broadcast_windows(channel)
    now = datetime(2026, 7, 2, 7, 30, tzinfo=UTC)  # = 16:30 JST (在-window)
    assert is_broadcast_paused(channel, now) is False


def test_is_broadcast_paused_false_when_unset(channel):
    from core.now_playing import is_broadcast_paused

    assert is_broadcast_paused(channel, timezone.now()) is False


def test_home_card_shows_paused_not_live_during_broadcast_window(channel, fixed_off_air_now):
    # 実運用で発覚した不具合の再現: agent は休止スレートを送出中 (online) でも、
    # ホームカードは「放送中」/LIVE バッジではなく「放送休止中」/live=False を返すべき。
    _set_broadcast_windows(channel)
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=fixed_off_air_now)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=fixed_off_air_now - timedelta(minutes=5),
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True},
    )

    c = Client().get("/api/v1/home").json()["cards"][0]
    assert c["online"] is True  # agent 自体は生存 (映像は配信中)
    assert c["live"] is False  # LIVE バッジは出さない
    assert c["nowtitle"] == "放送休止中"


def test_home_card_still_live_when_broadcasting_normally(channel, fixed_off_air_now):
    # broadcast_windows 未設定なら従来通り online → 「放送中」/live=True (回帰なし)。
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=fixed_off_air_now)

    c = Client().get("/api/v1/home").json()["cards"][0]
    assert c["online"] is True
    assert c["live"] is True
    assert c["nowtitle"] == "放送中"


def test_live_count_excludes_paused_channel(channel, fixed_off_air_now):
    from core.views import _live_count

    _set_broadcast_windows(channel)
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=fixed_off_air_now)

    assert _live_count(fixed_off_air_now) == 0


def test_player_channels_live_false_when_paused(channel, fixed_off_air_now):
    _set_broadcast_windows(channel)
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=fixed_off_air_now)

    data = Client().get("/api/v1/channels").json()
    assert data[0]["live"] is False


def test_player_channel_detail_live_false_when_paused(channel, fixed_off_air_now):
    _set_broadcast_windows(channel)
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=fixed_off_air_now)

    data = Client().get(f"/api/v1/channels/{channel.slug}").json()
    assert data["live"] is False


def test_channel_detail_suppresses_hls_when_paused(channel, fixed_off_air_now):
    # 休止中は公開プレイヤーがスレートを流し続けないよう hls_url を出さず、
    # paused / next_on_air (次回放送開始) を返す (自社フロントを実質オフライン化)。
    _set_broadcast_windows(channel)
    channel.cf_playback_hls_url = "https://cf.example/live.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=fixed_off_air_now)

    data = Client().get(f"/api/v1/channels/{channel.slug}").json()
    assert data["paused"] is True
    assert data["live"] is False
    assert data["hls_url"] is None
    assert data["next_on_air"] == "16:00"  # 13:38 JST の次の放送開始 = 16:00


def test_channel_detail_serves_hls_when_on_air(channel, monkeypatch):
    # 放送中は従来どおり hls_url を返し paused=False (回帰なし)。
    _set_broadcast_windows(channel)
    channel.cf_playback_hls_url = "https://cf.example/live.m3u8"
    channel.save(update_fields=["cf_playback_hls_url"])
    on_air = datetime(2026, 7, 2, 7, 30, tzinfo=UTC)  # = 16:30 JST (在-window)
    monkeypatch.setattr(timezone, "now", lambda: on_air)
    AgentStatus.objects.create(channel=channel, last_heartbeat_at=on_air)

    data = Client().get(f"/api/v1/channels/{channel.slug}").json()
    assert data["paused"] is False
    # #27: エッジ (Cloudflare Worker) 強制のため完全公開でも署名トークンが付く。
    assert data["hls_url"].startswith("https://cf.example/live.m3u8?token=")
    assert data["next_on_air"] is None
