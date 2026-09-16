# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SubscribeEvents 配信フィルタ (docs/operations.md 決定 O10)。

agent へは SCHEDULED (未実行) と CANCELLED (tombstone) のみ配信し、EXECUTING/DONE/
FAILED/SKIPPED は配信しない。とくに server が status=done で INSERT する割り込み記録
(feed 断自動退避などの ReportInterrupt 由来) がエコー配信されると、agent が実行済みの
スレートを再点火する二重実行が起きるため、それを構造的に防ぐ。
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from django.utils import timezone

from playout.grpc_service import _fetch_events_after
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus


def _mk(channel, status: str) -> PlayoutEvent:
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_ASSET,
        status=status,
    )


def test_fetch_delivers_only_scheduled_and_cancelled(channel):
    """SCHEDULED と CANCELLED だけが返り、実行系/完了系は除外される。"""
    sched = _mk(channel, PlayoutStatus.SCHEDULED)
    canc = _mk(channel, PlayoutStatus.CANCELLED)
    done = _mk(channel, PlayoutStatus.DONE)
    failed = _mk(channel, PlayoutStatus.FAILED)
    executing = _mk(channel, PlayoutStatus.EXECUTING)
    skipped = _mk(channel, PlayoutStatus.SKIPPED)

    got, _ = async_to_sync(_fetch_events_after)(channel.slug, 0, 100)
    keys = {e.idempotency_key for e in got}

    assert sched.idempotency_key in keys
    assert canc.idempotency_key in keys
    assert done.idempotency_key not in keys
    assert failed.idempotency_key not in keys
    assert executing.idempotency_key not in keys
    assert skipped.idempotency_key not in keys


def test_fetch_orders_by_sync_seq_and_respects_cursor(channel):
    """sync_seq 昇順で返し、cursor 以下は返さない。"""
    first = _mk(channel, PlayoutStatus.SCHEDULED)
    second = _mk(channel, PlayoutStatus.SCHEDULED)
    # sync_seq は BEFORE INSERT トリガーが採番する (昇順)。create() 後の in-memory
    # インスタンスには反映されないため DB から読み直す。
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.sync_seq is not None and second.sync_seq is not None
    assert second.sync_seq > first.sync_seq

    got, _ = async_to_sync(_fetch_events_after)(channel.slug, first.sync_seq, 100)
    keys = [e.idempotency_key for e in got]
    assert first.idempotency_key not in keys  # cursor と同値は除外 (sync_seq__gt)
    assert second.idempotency_key in keys


def test_fetch_scopes_to_channel(channel, db):
    """別チャンネルの event は混ざらない。"""
    from core.models import Channel

    other = Channel.objects.create(
        name="ICS-TV 2ch",
        slug="ch2",
        enabled=True,
        agent_token="other-token",  # pragma: allowlist secret - test only
    )
    mine = _mk(channel, PlayoutStatus.SCHEDULED)
    theirs = _mk(other, PlayoutStatus.SCHEDULED)

    got, _ = async_to_sync(_fetch_events_after)(channel.slug, 0, 100)
    keys = {e.idempotency_key for e in got}
    assert mine.idempotency_key in keys
    assert theirs.idempotency_key not in keys
