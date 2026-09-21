# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""運用画面 (緊急 SLATE 等) の HTTP smoke。"""

from __future__ import annotations

import pytest


def test_emergency_slate_requires_post(staff_client, channel):
    res = staff_client.get(f"/ops/ch/{channel.slug}/slate/")
    assert res.status_code == 405


def test_emergency_slate_creates_playout_event(staff_client, channel):
    from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus

    res = staff_client.post(f"/ops/ch/{channel.slug}/slate/")
    assert res.status_code in (200, 302)
    ev = PlayoutEvent.objects.get(channel=channel, action=PlayoutAction.PLAY_SLATE)
    assert ev.status == PlayoutStatus.SCHEDULED
    assert ev.params.get("reason") == "manual_emergency"


def test_emergency_slate_htmx_returns_inline_response(staff_client, channel):
    res = staff_client.post(f"/ops/ch/{channel.slug}/slate/", headers={"HX-Request": "true"})
    assert res.status_code == 200
    assert "slateEngaged" in res.headers.get("HX-Trigger", "")


def test_emergency_slate_unknown_channel_404(staff_client, db):
    res = staff_client.post("/ops/ch/nonexistent/slate/")
    assert res.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_resolver_does_not_cancel_slate_event(channel, asset_ready):
    """resolver が再解決しても緊急 SLATE event は CANCELLED にならない。"""
    import uuid
    from datetime import timedelta

    from django.utils import timezone

    from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
    from scheduling.resolver import resolve

    now = timezone.now()
    slate_key = uuid.uuid4()
    PlayoutEvent.objects.create(
        idempotency_key=slate_key,
        channel=channel,
        scheduled_at=now + timedelta(hours=1),
        action=PlayoutAction.PLAY_SLATE,
        params={"reason": "manual_emergency"},
        status=PlayoutStatus.SCHEDULED,
    )
    # resolver を 48h 窓で再解決 (program なし → 全部 filler を emit するが slate は触らない)
    resolve(channel, now, now + timedelta(hours=48))
    ev = PlayoutEvent.objects.get(idempotency_key=slate_key)
    assert ev.status == PlayoutStatus.SCHEDULED
