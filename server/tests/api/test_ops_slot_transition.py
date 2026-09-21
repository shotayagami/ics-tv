# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配信枠 (YT) 遷移を 🔴放送コンソール (ops) から実行する op_slot_transition のテスト (P1.3)。

studio の slot_transition_view と共有ロジック (admin_views.apply_slot_transition) を使うが、ops は
redirect ではなく HTMX 形式の 200 / エラー本文を返す (ops postForm が 200=成功で扱う)。エンドポイントは
slug でチャンネルを限定し、その配信枠だけを操作対象にする (放送ホストからの越権防止)。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


def _slot(channel, status="ready", broadcast_id="YT-1"):
    from youtube.models import YoutubeSlot

    now = timezone.now()
    return YoutubeSlot.objects.create(
        channel=channel,
        window_start=now,
        window_end=now + timedelta(hours=2),
        status=status,
        broadcast_id=broadcast_id,
        title="t",
    )


def _url(slug, slot_id):
    return f"/ops/ch/{slug}/slot/{slot_id}/transition/"


def test_ops_slot_transition_requires_staff(http_client, channel):
    slot = _slot(channel)
    res = http_client.post(_url(channel.slug, slot.id), {"target": "live"})
    assert res.status_code == 302  # staff_member_required → ログインへ


def test_ops_slot_transition_requires_post(staff_client, channel):
    slot = _slot(channel)
    res = staff_client.get(_url(channel.slug, slot.id))
    assert res.status_code == 405


def test_ops_slot_transition_to_live_returns_200(staff_client, channel, monkeypatch):
    slot = _slot(channel, status="testing")
    called = {}
    monkeypatch.setattr(
        "youtube.api.transition_broadcast",
        lambda ch, bid, target: called.update(bid=bid, target=target),
    )
    res = staff_client.post(_url(channel.slug, slot.id), {"target": "live"})
    assert res.status_code == 200  # redirect でなく HTMX _ok
    slot.refresh_from_db()
    assert slot.status == "live"
    assert called == {"bid": "YT-1", "target": "live"}


def test_ops_slot_transition_invalid_target_400(staff_client, channel):
    slot = _slot(channel)
    res = staff_client.post(_url(channel.slug, slot.id), {"target": "garbage"})
    assert res.status_code == 400


def test_ops_slot_transition_no_broadcast_id_412(staff_client, channel):
    slot = _slot(channel, broadcast_id="")
    res = staff_client.post(_url(channel.slug, slot.id), {"target": "live"})
    assert res.status_code == 412


def test_ops_slot_transition_other_channel_404(staff_client, channel, db):
    """slug で限定するので、別チャンネルの枠 id を渡しても 404 (越権防止)。"""
    from core.models import Channel

    other = Channel.objects.create(name="ch2", slug="ch2", enabled=True)
    slot = _slot(other)
    res = staff_client.post(_url(channel.slug, slot.id), {"target": "live"})
    assert res.status_code == 404
