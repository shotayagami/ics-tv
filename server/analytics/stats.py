# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴計測の集計 (#ADMIN-02)。同時接続数・番組別ユニーク視聴・人気ランキング。"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncHour
from django.utils import timezone

from analytics.models import AccessLogEntry, ViewerPresence

# 直近この秒数に beat があれば「視聴中」とみなす (heartbeat 間隔 30s の余裕を見て 90s)。
PRESENCE_WINDOW_SEC = 90


def concurrent_by_channel(now=None) -> dict[int, int]:
    """channel_id → 直近 PRESENCE_WINDOW 秒の同時視聴者数。スタッフ可視化用。"""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=PRESENCE_WINDOW_SEC)
    rows = (
        ViewerPresence.objects.filter(last_seen__gte=cutoff)
        .values("channel_id")
        .annotate(n=Count("id"))
    )
    return {r["channel_id"]: r["n"] for r in rows}


def concurrent_total(now=None) -> int:
    """全 ch の同時視聴者数 (distinct viewer×channel の在席)。"""
    return sum(concurrent_by_channel(now).values())


def top_programs(limit: int = 15):
    """ユニーク視聴者数の多い公開番組 (上位)。スタッフ集計用。number は n 属性。"""
    from scheduling.models import Program

    return list(
        Program.objects.filter(public_visible=True)
        .annotate(n=Count("views"))
        .filter(n__gt=0)
        .select_related("channel", "series")
        .order_by("-n", "-start_at")[:limit]
    )


def popular_vod(limit: int = 8, now=None):
    """見逃し再生できる番組をユニーク視聴者数 (放送時の人気) の多い順に (#DISC-01 ランキング)。

    視聴データが無ければ空 → 呼び出し側でセクションごと非表示 (degrade)。
    """
    from scheduling import vod as vod_mod

    qs = (
        vod_mod.available_vod_qs(now)
        .annotate(n=Count("views"))
        .filter(n__gt=0)
        .order_by("-n", "-end_at")
    )
    return list(qs[:limit])


# ---- HTTP アクセスログ集計 (awstats 的な画面。studio/backoffice 共通) ----


def access_hosts(since=None) -> list[str]:
    """直近に記録があるホスト一覧 (画面のホスト選択に使う)。多い順。"""
    qs = AccessLogEntry.objects.all()
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    rows = qs.values("host").annotate(n=Count("id")).order_by("-n")
    return [r["host"] for r in rows]


def access_hourly(host: str, since, until=None) -> list[dict]:
    """host の時間帯別ヒット数 (Asia/Tokyo の時単位)。[{label, count}] を時系列順で返す。"""
    qs = AccessLogEntry.objects.filter(host=host, created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)
    rows = (
        qs.annotate(hour=TruncHour("created_at", tzinfo=timezone.get_current_timezone()))
        .values("hour")
        .annotate(n=Count("id"))
        .order_by("hour")
    )
    return [{"label": r["hour"].strftime("%m/%d %H時"), "count": r["n"]} for r in rows]


def access_top_paths(host: str, since, limit: int = 20) -> list[dict]:
    """host のアクセス数上位パス。"""
    rows = (
        AccessLogEntry.objects.filter(host=host, created_at__gte=since)
        .values("path")
        .annotate(n=Count("id"))
        .order_by("-n")[:limit]
    )
    return [{"label": r["path"], "count": r["n"]} for r in rows]


def access_status_breakdown(host: str, since) -> list[dict]:
    """host のステータスコード帯 (2xx/3xx/4xx/5xx/101) 別ヒット数。"""
    rows = AccessLogEntry.objects.filter(host=host, created_at__gte=since).values_list(
        "status", flat=True
    )
    buckets: dict[str, int] = {}
    for status in rows:
        label = "101 (WS)" if status == 101 else f"{status // 100}xx"
        buckets[label] = buckets.get(label, 0) + 1
    return [{"label": k, "count": v} for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])]


def access_summary(host: str, since) -> dict:
    """1 host のダッシュボード用サマリ (studio/backoffice 共通画面)。"""
    qs = AccessLogEntry.objects.filter(host=host, created_at__gte=since)
    return {
        "host": host,
        "total_hits": qs.count(),
        "total_pages": qs.filter(is_page=True).count(),
        "hourly": access_hourly(host, since),
        "top_paths": access_top_paths(host, since),
        "status": access_status_breakdown(host, since),
    }
