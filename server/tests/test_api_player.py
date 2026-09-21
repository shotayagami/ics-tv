# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 1: 公開プレイヤー島 API (channels / channel detail)。"""

from datetime import timedelta

from django.test import Client
from django.utils import timezone


def test_channels_list(channel):
    data = Client().get("/api/v1/channels").json()
    c = next(x for x in data if x["slug"] == "ch1")
    assert set(c) >= {"slug", "name", "short", "tint", "live", "pinned"}
    assert c["pinned"] is False


def test_channel_detail_no_programs(channel):
    resp = Client().get("/api/v1/channels/ch1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["slug"] == "ch1"
    assert d["current"] is None
    assert d["day_list"] == []
    assert d["is_pinned"] is False


def test_channel_detail_404(db):
    assert Client().get("/api/v1/channels/nope").status_code == 404


def test_channel_detail_with_current_program(channel, asset_ready):
    from scheduling.models import Program

    now = timezone.now()
    # chk_program_source: recorded は asset 必須 (conftest の asset_ready を使う)
    Program.objects.create(
        channel=channel,
        title="現在番組",
        type="recorded",
        asset=asset_ready,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
    )
    d = Client().get("/api/v1/channels/ch1").json()
    assert d["current"] is not None
    assert d["current"]["title"] == "現在番組"
    assert isinstance(d["current"]["start_ts"], int)
    assert len(d["day_list"]) == 1
    assert d["day_list"][0]["is_now"] is True
