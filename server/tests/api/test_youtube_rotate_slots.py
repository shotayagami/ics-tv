# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rolling 枠 beat (rotate_slots) の単体テスト。

YouTube API ラッパー (transition_broadcast / livestream_active / broadcast_lifecycle) を
monkeypatch し、状態遷移と transition 失敗時の自己回復 (5xx=deferred 再試行 / 4xx=実状態追認 /
窓超過=missed) を検証する。2026-07-06 の「go-live 時 503 → 朝枠 4h 無配信」の再発防止。
"""

from __future__ import annotations

import json
from datetime import timedelta

import httplib2
import pytest
from django.utils import timezone
from googleapiclient.errors import HttpError

from youtube import tasks
from youtube.models import YoutubeSlot, YtSlotStatus

pytestmark = pytest.mark.django_db


def _http_error(status: int, reason: str = "backendError") -> HttpError:
    resp = httplib2.Response({"status": status, "reason": reason})
    content = json.dumps(
        {"error": {"errors": [{"reason": reason}], "code": status, "message": reason}}
    ).encode()
    return HttpError(resp, content)


def _slot(channel, *, start, end, status, bid="BC"):
    return YoutubeSlot.objects.create(
        channel=channel,
        window_start=start,
        window_end=end,
        status=status,
        broadcast_id=bid,
    )


@pytest.fixture
def stream_active(monkeypatch):
    monkeypatch.setattr("youtube.tasks.livestream_active", lambda ch, lid=None: True)


def test_rotate_to_live(channel, stream_active, monkeypatch):
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )
    calls = []
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast", lambda ch, bid, t: calls.append((bid, t))
    )

    stats = tasks.rotate_slots(channel.id)
    assert stats["to_live"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.LIVE
    assert calls == [("BC", "live")]


def test_rotate_to_complete(channel, stream_active, monkeypatch):
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(hours=4),
        end=now - timedelta(minutes=1),
        status=YtSlotStatus.LIVE,
    )
    calls = []
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast", lambda ch, bid, t: calls.append((bid, t))
    )

    stats = tasks.rotate_slots(channel.id)
    assert stats["to_complete"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.COMPLETE
    assert calls == [("BC", "complete")]


def test_transient_5xx_on_live_keeps_ready_and_retries(channel, stream_active, monkeypatch):
    """5xx は ERROR 確定にせず READY のまま残り、翌分の rotate が再試行して成功する。"""
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    def boom(ch, bid, t):
        raise _http_error(503, "SERVICE_UNAVAILABLE")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)
    monkeypatch.setattr(
        "youtube.tasks.broadcast_lifecycle",
        lambda *a, **k: pytest.fail("5xx では実状態確認しない"),
    )

    stats = tasks.rotate_slots(channel.id)
    assert stats["deferred"] == 1
    assert stats["errors"] == 0
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.READY  # 再試行対象のまま
    assert "503" in slot.error

    # 翌分: YouTube 復旧 → live 化に成功し error もクリア
    monkeypatch.setattr("youtube.tasks.transition_broadcast", lambda *a, **k: None)
    stats = tasks.rotate_slots(channel.id)
    assert stats["to_live"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.LIVE
    assert slot.error is None


def test_transient_5xx_on_complete_keeps_live(channel, stream_active, monkeypatch):
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(hours=4),
        end=now - timedelta(minutes=1),
        status=YtSlotStatus.LIVE,
    )

    def boom(ch, bid, t):
        raise _http_error(503, "SERVICE_UNAVAILABLE")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)

    stats = tasks.rotate_slots(channel.id)
    assert stats["deferred"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.LIVE  # 翌分の finishing で再試行される


def test_invalid_transition_on_live_reconciles_already_live(channel, stream_active, monkeypatch):
    """YT Studio 手動 go-live 済み等: 4xx でも実状態が live なら追認する。"""
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    def boom(ch, bid, t):
        raise _http_error(403, "invalidTransition")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)
    monkeypatch.setattr("youtube.tasks.broadcast_lifecycle", lambda ch, bid: "live")

    stats = tasks.rotate_slots(channel.id)
    assert stats["to_live"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.LIVE
    assert slot.error is None


@pytest.mark.parametrize("lifecycle", ["complete", "revoked", None])
def test_invalid_transition_on_complete_reconciles(channel, stream_active, monkeypatch, lifecycle):
    """既に終了済み / 削除済みの broadcast への complete は COMPLETE 追認 (2026-07-06 の実例)。"""
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(hours=4),
        end=now - timedelta(minutes=1),
        status=YtSlotStatus.LIVE,
    )

    def boom(ch, bid, t):
        raise _http_error(403, "invalidTransition")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)
    monkeypatch.setattr("youtube.tasks.broadcast_lifecycle", lambda ch, bid: lifecycle)

    stats = tasks.rotate_slots(channel.id)
    assert stats["to_complete"] == 1
    assert stats["errors"] == 0
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.COMPLETE


def test_unreconcilable_4xx_marks_error(channel, stream_active, monkeypatch):
    """実状態でも追認できない 4xx は従来どおり ERROR 確定。"""
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    def boom(ch, bid, t):
        raise _http_error(403, "invalidTransition")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)
    monkeypatch.setattr("youtube.tasks.broadcast_lifecycle", lambda ch, bid: "ready")

    stats = tasks.rotate_slots(channel.id)
    assert stats["errors"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.ERROR


def test_missed_window_ready_marked_error(channel, stream_active, monkeypatch):
    """窓を live 化されないまま過ぎた READY は ERROR に落として可視化する。"""
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(hours=8),
        end=now - timedelta(hours=4),
        status=YtSlotStatus.READY,
    )
    monkeypatch.setattr(
        "youtube.tasks.transition_broadcast",
        lambda *a, **k: pytest.fail("窓外の枠は transition しない"),
    )

    stats = tasks.rotate_slots(channel.id)
    assert stats["missed"] == 1
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.ERROR
    assert "窓を通過" in slot.error


def test_in_window_ready_not_swept_by_missed(channel, stream_active, monkeypatch):
    """5xx deferred 中 (窓内 READY) の枠を missed 掃除が誤って ERROR にしない。"""
    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    def boom(ch, bid, t):
        raise _http_error(500, "backendError")

    monkeypatch.setattr("youtube.tasks.transition_broadcast", boom)

    stats = tasks.rotate_slots(channel.id)
    assert stats["missed"] == 0
    slot.refresh_from_db()
    assert slot.status == YtSlotStatus.READY


def test_missed_fires_crit_notification(channel, stream_active, monkeypatch):
    """missed (窓落とし=無配信確定) は CRIT の運用通知を発火する (2026-09-02 監査 決定#1)。"""
    from core.models import Notification, NotificationSeverity

    now = timezone.now()
    slot = _slot(
        channel,
        start=now - timedelta(hours=8),
        end=now - timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    stats = tasks.rotate_slots(channel.id)
    assert stats["missed"] == 1
    n = Notification.objects.get(kind="yt_rotate_missed")
    assert n.severity == NotificationSeverity.CRIT
    assert n.channel_id == channel.id
    assert str(slot.id) in n.message
    assert channel.slug in n.message


def test_blocked_fires_crit_notification(channel, monkeypatch):
    """blocked (liveStream 非 active で live 化できない) も CRIT の運用通知を発火する。"""
    from core.models import Notification, NotificationSeverity

    monkeypatch.setattr("youtube.tasks.livestream_active", lambda ch, lid=None: False)
    now = timezone.now()
    _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    stats = tasks.rotate_slots(channel.id)
    assert stats["blocked"] == 1
    n = Notification.objects.get(kind="yt_rotate_blocked")
    assert n.severity == NotificationSeverity.CRIT
    assert "liveStream" in n.message


def test_rotate_notification_deduped_within_cooldown(channel, monkeypatch):
    """rotate は毎分回るため、同一 (kind, channel) の通知はクールダウン内 1 回に抑える。"""
    from core.models import Notification

    monkeypatch.setattr("youtube.tasks.livestream_active", lambda ch, lid=None: False)
    now = timezone.now()
    _slot(
        channel,
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=4),
        status=YtSlotStatus.READY,
    )

    tasks.rotate_slots(channel.id)  # 1 tick 目: 通知
    tasks.rotate_slots(channel.id)  # 2 tick 目: 同一異常の継続 → 抑止
    assert Notification.objects.filter(kind="yt_rotate_blocked").count() == 1

    # クールダウンを過ぎれば再通知する (異常が続いていることを見失わない)
    Notification.objects.filter(kind="yt_rotate_blocked").update(
        created_at=now - tasks._ROTATE_NOTIFY_COOLDOWN - timedelta(minutes=1)
    )
    tasks.rotate_slots(channel.id)
    assert Notification.objects.filter(kind="yt_rotate_blocked").count() == 2
