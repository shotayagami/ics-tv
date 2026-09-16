# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-12): YouTube スロット dashboard admin API。

staff_auth ゲート + 2h×12 ローリングスロットの一覧 (枠/状態/broadcast/タイトル)。状態遷移/メタ更新/
削除/テンプレ適用の操作は既存 admin-ui エンドポイント (tests/api/test_youtube*) の責務。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from youtube.models import YoutubeSlot, YtSlotStatus

_URL = "/api/v1/admin/youtube/{slug}/slots"


def test_slots_requires_auth(http_client, channel, db):
    assert http_client.get(_URL.format(slug=channel.slug)).status_code == 401


def test_slots_unknown_channel_404(staff_client, db):
    assert staff_client.get(_URL.format(slug="nope")).status_code == 404


def test_slots_list(staff_client, channel, db):
    now = timezone.now()
    YoutubeSlot.objects.create(
        channel=channel,
        window_start=now,
        window_end=now + timedelta(hours=2),
        broadcast_id="yt-x",
        status=YtSlotStatus.LIVE,
        title="配信枠タイトル",
    )
    d = staff_client.get(_URL.format(slug=channel.slug)).json()
    assert d["channel"]["slug"] == channel.slug
    assert d["settings_url"].endswith(f"/admin-ui/channels/{channel.slug}/settings/")
    row = next(s for s in d["slots"] if s["broadcast_id"] == "yt-x")
    assert row["status"] == "live" and row["title"] == "配信枠タイトル"
    assert "–" in row["window"]  # m/d HH:MM–HH:MM
