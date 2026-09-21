# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""members の非同期タスク (#EPG-03 リマインド)。

send_due_reminders: 開始 lead_minutes 分前に達した未送信のリマインド予約をメール送信し、
notified_at を記録して一度だけ送る。開始済み番組は送らない (遅延メール抑止)。Celery Beat が周期実行。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from members import notifications
from members.models import Reminder

logger = logging.getLogger(__name__)


@shared_task
def send_due_reminders(lead_minutes: int = 30) -> dict:
    now = timezone.now()
    horizon = now + timedelta(minutes=lead_minutes)
    due = Reminder.objects.filter(
        notified_at__isnull=True,
        program__start_at__gt=now,
        program__start_at__lte=horizon,
    ).select_related("member", "program", "program__channel")
    sent = 0
    for r in due:
        if not (r.member.is_active and r.member.is_verified):
            continue  # 退会/未確認はメール送らない
        try:
            notifications.send_program_reminder(r.member, r.program)
        except Exception:
            logger.warning(
                "reminder send failed: member=%s program=%s",
                r.member_id,
                r.program_id,
                exc_info=True,
            )
            continue
        Reminder.objects.filter(pk=r.pk).update(notified_at=now)
        sent += 1
    if sent:
        logger.info("send_due_reminders: sent=%s", sent)
    return {"sent": sent}
