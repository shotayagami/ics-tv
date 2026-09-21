# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA 向け HTTP アクセス統計 (awstats 的な画面)。staff 限定。

studio.* / backoffice.* から共通で参照する単一画面のデータ源 (backoffice はこの画面への
リンクを持つだけで、集計は複製しない)。analytics.stats の読み取り集計を JSON 化する。
"""

from __future__ import annotations

from datetime import timedelta

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from api.auth import staff_auth
from api.schemas import AccessStatsOut

router = Router(tags=["admin"], auth=staff_auth)


@router.get("/admin/access-stats", response=AccessStatsOut)
def access_stats(request: HttpRequest, host: str = "", range_hours: int = 24):
    from analytics import stats

    range_hours = max(1, min(range_hours, 24 * 35))
    since = timezone.now() - timedelta(hours=range_hours)
    hosts = stats.access_hosts(since=since)
    selected = host if host in hosts else (hosts[0] if hosts else "")
    summary = (
        stats.access_summary(selected, since)
        if selected
        else {
            "host": "",
            "total_hits": 0,
            "total_pages": 0,
            "hourly": [],
            "top_paths": [],
            "status": [],
        }
    )
    return {**summary, "hosts": hosts, "range_hours": range_hours}
