# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""放送中スレート固着の検出 beat (playout.tasks.check_stuck_slate)。

スレート層 (layer 90) は本線 (layer 10) と独立なので、本線が正常に流れていても画面はスレートの
まま=視聴者には停波と同じになる。それでも送出イベントは成功し続けるため、既存の死活 beat にも
送出失敗通知にも掛からない。2026-07-21 06:00 の休止明けでは 2h21m 誰も気付かず、運用者が手動
解除するまで復旧しなかった。この beat がその盲点を埋める。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import Notification
from playout.models import AgentStatus, PlayoutAction, PlayoutEvent, PlayoutStatus
from playout.tasks import STUCK_SLATE_GRACE_SEC, check_stuck_slate

pytestmark = pytest.mark.django_db(transaction=True)


def _windows(channel):
    """00:00-24:00 を放送中にする (窓内=on-air 判定を常に True にする)。"""
    channel.broadcast_windows = [{"start": "00:00", "end": "24:00"}]
    channel.save(update_fields=["broadcast_windows"])


def _off_air_windows(channel):
    """現在時刻が確実に窓外 (休止中) になる窓を組む。"""
    channel.broadcast_windows = [{"start": "00:00", "end": "00:01"}]
    channel.save(update_fields=["broadcast_windows"])


def _status(channel, *, slate_active=True, feed_state="idle"):
    return AgentStatus.objects.create(
        channel=channel,
        last_heartbeat_at=timezone.now(),
        slate_active=slate_active,
        feed_state=feed_state,
    )


def _slate(channel, *, params, ago_sec):
    at = timezone.now() - timedelta(seconds=ago_sec)
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=at,
        action=PlayoutAction.PLAY_SLATE,
        params=params,
        status=PlayoutStatus.DONE,
        actual_at=at,
    )


_OLD = STUCK_SLATE_GRACE_SEC + 60


def test_detects_off_air_slate_stuck_during_on_air(channel):
    """放送中に resolver 管轄スレートが猶予を超えて残っていれば CRIT 通知 (本不具合の検知)。"""
    _windows(channel)
    _status(channel)
    _slate(channel, params={"off_air": True, "loop": True}, ago_sec=_OLD)

    res = check_stuck_slate()

    assert res["stuck"] == 1
    assert AgentStatus.objects.get(channel=channel).slate_stuck_notified is True
    assert Notification.objects.filter(kind="slate_stuck", severity="crit").count() == 1


def test_does_not_double_notify_then_clears(channel):
    """遷移時のみ通知し、解消したら INFO で復旧通知してフラグを戻す。"""
    _windows(channel)
    st = _status(channel)
    _slate(channel, params={"off_air": True, "loop": True}, ago_sec=_OLD)
    check_stuck_slate()

    # 継続中は再通知しない
    assert check_stuck_slate()["stuck"] == 0
    assert Notification.objects.filter(kind="slate_stuck").count() == 1

    # スレート解除 → 復旧通知
    AgentStatus.objects.filter(pk=st.pk).update(slate_active=False)
    res = check_stuck_slate()
    assert res["cleared"] == 1
    assert AgentStatus.objects.get(channel=channel).slate_stuck_notified is False
    assert Notification.objects.filter(kind="slate_stuck_cleared", severity="info").count() == 1


def test_no_alert_while_off_air(channel):
    """休止中はスレートが出ているのが正常なので鳴らさない。"""
    _off_air_windows(channel)
    _status(channel)
    _slate(channel, params={"off_air": True, "loop": True}, ago_sec=_OLD)

    assert check_stuck_slate()["stuck"] == 0
    assert not Notification.objects.filter(kind="slate_stuck").exists()


def test_no_alert_for_manual_operator_slate(channel):
    """運用者の手動/緊急スレート (off_air param 無し) は運用意図なので鳴らさない。"""
    _windows(channel)
    _status(channel)
    _slate(channel, params={"reason": "manual_emergency"}, ago_sec=_OLD)

    assert check_stuck_slate()["stuck"] == 0
    assert not Notification.objects.filter(kind="slate_stuck").exists()


def test_no_alert_when_feed_lost(channel):
    """feed 断由来のスレートは SLATE_ON / feed_lost で別途通知済みなので二重に鳴らさない。"""
    _windows(channel)
    _status(channel, feed_state="lost")
    _slate(channel, params={"off_air": True, "loop": True}, ago_sec=_OLD)

    assert check_stuck_slate()["stuck"] == 0
    assert not Notification.objects.filter(kind="slate_stuck").exists()


def test_no_alert_within_grace_period(channel):
    """猶予内は鳴らさない (休止明けの解除や resolver の自己修復が働く余地を残す)。"""
    _windows(channel)
    _status(channel)
    _slate(channel, params={"off_air": True, "loop": True}, ago_sec=STUCK_SLATE_GRACE_SEC - 60)

    assert check_stuck_slate()["stuck"] == 0
    assert not Notification.objects.filter(kind="slate_stuck").exists()


def test_no_alert_when_slate_not_active(channel):
    """agent がスレートを出していなければ (slate_active=False) 何も起きない。"""
    _windows(channel)
    _status(channel, slate_active=False)
    _slate(channel, params={"off_air": True, "loop": True}, ago_sec=_OLD)

    assert check_stuck_slate()["stuck"] == 0
    assert not Notification.objects.filter(kind="slate_stuck").exists()
