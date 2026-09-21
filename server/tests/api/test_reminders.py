# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴リマインド (#EPG-03): 予約トグル + 開始前メール送信タスク。"""

from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from members.models import Member, Reminder
from members.tasks import send_due_reminders
from scheduling.models import Program, ProgramType

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_LOCMEM = override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _member(email="m@example.com", verified=True):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _program(channel, asset, *, start_delta, title="番組X"):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
        public_visible=True,
    )


# ---- トグル ----


@_PUBLIC_HOST
def test_toggle_creates_and_deletes(http_client, channel, asset_ready, db):
    m = _member()
    _login(http_client)
    p = _program(channel, asset_ready, start_delta=timedelta(hours=2))
    r1 = http_client.post(f"/members/reminders/{p.id}/toggle/", **_AJAX)
    assert r1.status_code == 200 and r1.json()["reminded"] is True
    assert Reminder.objects.filter(member=m, program=p).exists()
    r2 = http_client.post(f"/members/reminders/{p.id}/toggle/", **_AJAX)
    assert r2.json()["reminded"] is False
    assert not Reminder.objects.filter(member=m, program=p).exists()


@_PUBLIC_HOST
def test_toggle_requires_verified(http_client, channel, asset_ready, db):
    _member(verified=False)
    _login(http_client)
    p = _program(channel, asset_ready, start_delta=timedelta(hours=2))
    res = http_client.post(f"/members/reminders/{p.id}/toggle/")
    assert res.status_code == 302 and res.url == "/members/verify-required/"
    assert not Reminder.objects.exists()


# ---- 送信タスク ----


@_PUBLIC_HOST
@_LOCMEM
def test_task_sends_due_then_marks_notified(http_client, channel, asset_ready, db):
    m = _member()
    p = _program(channel, asset_ready, start_delta=timedelta(minutes=20))  # lead(30)以内
    rem = Reminder.objects.create(member=m, program=p)
    mail.outbox.clear()
    assert send_due_reminders(lead_minutes=30) == {"sent": 1}
    assert len(mail.outbox) == 1 and p.title in mail.outbox[0].subject
    rem.refresh_from_db()
    assert rem.notified_at is not None
    # 二度目は送らない (notified 済み)
    assert send_due_reminders(lead_minutes=30) == {"sent": 0}


@_LOCMEM
def test_task_skips_far_future(channel, asset_ready, db):
    m = _member()
    p = _program(channel, asset_ready, start_delta=timedelta(hours=2))  # lead 外
    Reminder.objects.create(member=m, program=p)
    mail.outbox.clear()
    assert send_due_reminders(lead_minutes=30) == {"sent": 0}
    assert len(mail.outbox) == 0


@_LOCMEM
def test_task_skips_already_started(channel, asset_ready, db):
    m = _member()
    p = _program(channel, asset_ready, start_delta=timedelta(minutes=-5))  # 開始済み
    Reminder.objects.create(member=m, program=p)
    assert send_due_reminders(lead_minutes=30) == {"sent": 0}


@_LOCMEM
def test_task_skips_unverified(channel, asset_ready, db):
    m = _member(verified=False)
    p = _program(channel, asset_ready, start_delta=timedelta(minutes=20))
    Reminder.objects.create(member=m, program=p)
    assert send_due_reminders(lead_minutes=30) == {"sent": 0}
    assert len(mail.outbox) == 0
