# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴計測の周期タスク (#ADMIN-02)。在席の掃除 (ProgramView は累積保持)。"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task
def prune_stale_presence(max_age_minutes: int = 10) -> int:
    """古い在席 (last_seen が max_age 分以上前) を削除。在席は短命なので溜めない。"""
    from analytics.models import ViewerPresence

    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    deleted, _ = ViewerPresence.objects.filter(last_seen__lt=cutoff).delete()
    return deleted


@shared_task
def prune_access_log(retention_days: int = 35) -> int:
    """古い AccessLogEntry (retention_days より前) を削除。awstats 的な直近集計にしか使わないため累積保持しない。"""
    from analytics.models import AccessLogEntry

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted, _ = AccessLogEntry.objects.filter(created_at__lt=cutoff).delete()
    return deleted
