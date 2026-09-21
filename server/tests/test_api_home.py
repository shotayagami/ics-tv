# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2b: 公開トップ ライブカード API。"""

from datetime import timedelta

from django.test import Client
from django.utils import timezone


def test_home_empty(db):
    d = Client().get("/api/v1/home").json()
    assert d["featured"] is None
    assert d["cards"] == []


def test_home_card_fields(channel):
    d = Client().get("/api/v1/home").json()
    assert len(d["cards"]) == 1
    c = d["cards"][0]
    assert c["slug"] == "ch1"
    assert set(c) >= {"slug", "name", "online", "live", "nowtitle", "hls_url", "poster"}
    assert d["featured"]["slug"] == "ch1"  # 先頭 ch がヒーロー


def test_home_card_current_program(channel, asset_ready):
    from scheduling.models import Program

    now = timezone.now()
    Program.objects.create(
        channel=channel,
        title="ホーム現在",
        type="recorded",
        asset=asset_ready,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        public_visible=True,
    )
    c = Client().get("/api/v1/home").json()["cards"][0]
    assert c["nowtitle"] == "ホーム現在"  # 実出力なし → current.title へフォールバック
    assert isinstance(c["cur_start_ts"], int)
