# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴計測 (#ADMIN-02): ハートビート / 集計 / prune / 人気ランキング / スタッフ計測ダッシュボード。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from analytics import stats
from analytics.models import AccessLogEntry, ProgramView, ViewerPresence
from analytics.tasks import prune_access_log, prune_stale_presence
from scheduling.models import Program, ProgramType, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _airing_now(channel, asset, *, title="放送中番組"):
    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=20),
        asset=asset,
        public_visible=True,
    )


def _vod(channel, asset, *, title, ended_ago):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=VodVisibility.PUBLIC,
    )


# ---- ハートビート ----


@_PUBLIC_HOST
def test_beat_records_presence_and_view(http_client, channel, asset_ready, db):
    p = _airing_now(channel, asset_ready)
    res = http_client.post("/api/beat", {"ch": "ch1"})
    assert res.status_code == 200 and res.json()["ok"] is True
    assert "icstv_vid" in res.cookies  # 匿名 id を発行
    assert ViewerPresence.objects.filter(channel=channel).count() == 1
    assert ProgramView.objects.filter(program=p).count() == 1


@_PUBLIC_HOST
def test_beat_unknown_channel_400(http_client, db):
    assert http_client.post("/api/beat", {"ch": "nope"}).status_code == 400


@_PUBLIC_HOST
def test_beat_requires_post(http_client, channel, db):
    assert http_client.get("/api/beat").status_code == 405


@_PUBLIC_HOST
def test_beat_same_viewer_is_idempotent(http_client, channel, asset_ready, db):
    p = _airing_now(channel, asset_ready)
    http_client.post("/api/beat", {"ch": "ch1"})  # cookie 発行
    http_client.post("/api/beat", {"ch": "ch1"})  # 同一 viewer の再 beat
    assert ProgramView.objects.filter(program=p).count() == 1  # ユニーク視聴は1
    assert ViewerPresence.objects.filter(channel=channel).count() == 1  # 在席は upsert


# ---- 集計 / prune ----


def test_concurrent_excludes_stale(channel, db):
    now = timezone.now()
    ViewerPresence.objects.create(viewer_id="fresh", channel=channel, last_seen=now)
    ViewerPresence.objects.create(
        viewer_id="stale", channel=channel, last_seen=now - timedelta(seconds=200)
    )
    assert stats.concurrent_by_channel(now).get(channel.id) == 1  # 直近のみ


def test_prune_stale_presence(channel, db):
    now = timezone.now()
    ViewerPresence.objects.create(viewer_id="fresh", channel=channel, last_seen=now)
    ViewerPresence.objects.create(
        viewer_id="old", channel=channel, last_seen=now - timedelta(minutes=30)
    )
    prune_stale_presence(max_age_minutes=10)
    assert ViewerPresence.objects.filter(channel=channel).count() == 1
    assert ViewerPresence.objects.filter(viewer_id="fresh").exists()


# ---- 人気ランキング (#DISC-01 解禁) ----


def test_popular_vod_orders_by_views_excludes_zero(channel, asset_ready, db):
    hi = _vod(channel, asset_ready, title="人気番組", ended_ago=timedelta(hours=2))
    lo = _vod(channel, asset_ready, title="そこそこ番組", ended_ago=timedelta(hours=5))
    zero = _vod(channel, asset_ready, title="無視聴番組", ended_ago=timedelta(hours=8))
    for i in range(3):
        ProgramView.objects.create(program=hi, viewer_id=f"v{i}")
    ProgramView.objects.create(program=lo, viewer_id="v0")
    ids = [p.id for p in stats.popular_vod()]
    assert ids[:2] == [hi.id, lo.id]  # 視聴数の多い順
    assert zero.id not in ids  # 無視聴は出さない (degrade)


# ---- スタッフ計測ダッシュボード ----


def test_analytics_dashboard_staff(staff_client, channel, asset_ready, db):
    p = _vod(channel, asset_ready, title="計測番組", ended_ago=timedelta(hours=2))
    ProgramView.objects.create(program=p, viewer_id="v1")
    body = staff_client.get(f"/ops/ch/{channel.slug}/analytics/").content.decode("utf-8")
    assert "視聴計測" in body and "計測番組" in body and "ユニーク視聴" in body


def test_analytics_dashboard_blocks_anon(http_client, channel, db):
    res = http_client.get(f"/ops/ch/{channel.slug}/analytics/")
    assert res.status_code in (302, 403)  # staff_member_required


# ---- HTTP アクセスログ (awstats 的な画面。studio/backoffice 共通) ----


def test_access_log_middleware_records_hit(http_client, db):
    before = AccessLogEntry.objects.count()
    res = http_client.get("/api/v1/health/")
    assert res.status_code == 200
    entry = AccessLogEntry.objects.latest("id")
    assert AccessLogEntry.objects.count() == before + 1
    assert entry.host == "testserver"  # Django test client の既定 Host
    assert entry.method == "GET"
    assert entry.path == "/api/v1/health/"
    assert entry.status == 200
    assert entry.is_page is False  # /api/ 配下は非ページ扱い


def test_access_log_middleware_marks_real_pages(http_client, db):
    http_client.get("/")  # studio.* トップ (リダイレクト含め「ページ」扱い)
    entry = AccessLogEntry.objects.latest("id")
    assert entry.path == "/"
    assert entry.is_page is True


def test_access_log_middleware_skips_static(http_client, db):
    before = AccessLogEntry.objects.count()
    http_client.get("/static/does-not-exist.js")
    assert AccessLogEntry.objects.count() == before  # 静的配信は記録しない


def _access_entry(
    host: str, path: str, *, status: int = 200, hours_ago: int = 0, is_page: bool = True
):
    e = AccessLogEntry.objects.create(
        host=host, method="GET", path=path, status=status, is_page=is_page
    )
    AccessLogEntry.objects.filter(pk=e.pk).update(
        created_at=timezone.now() - timedelta(hours=hours_ago)
    )
    return e


def test_access_hosts_orders_by_volume(db):
    _access_entry("tv.yagamin.net", "/")
    _access_entry("studio.yagamin.net", "/studio/")
    _access_entry("studio.yagamin.net", "/studio/channels")
    assert stats.access_hosts() == ["studio.yagamin.net", "tv.yagamin.net"]


def test_access_hourly_buckets_by_local_hour(db):
    _access_entry("tv.yagamin.net", "/", hours_ago=0)
    _access_entry("tv.yagamin.net", "/", hours_ago=0)
    _access_entry("tv.yagamin.net", "/", hours_ago=5)
    since = timezone.now() - timedelta(hours=24)
    rows = stats.access_hourly("tv.yagamin.net", since)
    assert sum(r["count"] for r in rows) == 3
    assert len(rows) == 2  # 直近時間 と 5時間前 の2バケット


def test_access_top_paths_and_status_breakdown(db):
    _access_entry("tv.yagamin.net", "/ch/ch1/", status=200)
    _access_entry("tv.yagamin.net", "/ch/ch1/", status=200)
    _access_entry("tv.yagamin.net", "/vod/1/", status=404)
    since = timezone.now() - timedelta(hours=24)
    top = stats.access_top_paths("tv.yagamin.net", since)
    assert top[0] == {"label": "/ch/ch1/", "count": 2}
    statuses = {
        r["label"]: r["count"] for r in stats.access_status_breakdown("tv.yagamin.net", since)
    }
    assert statuses == {"2xx": 2, "4xx": 1}


def test_access_summary_excludes_other_hosts(db):
    _access_entry("tv.yagamin.net", "/", is_page=True)
    _access_entry("tv.yagamin.net", "/api/v1/home", is_page=False)
    _access_entry("studio.yagamin.net", "/studio/", is_page=True)
    since = timezone.now() - timedelta(hours=24)
    s = stats.access_summary("tv.yagamin.net", since)
    assert s["total_hits"] == 2
    assert s["total_pages"] == 1


def test_prune_access_log_removes_old_entries(db):
    old = _access_entry("tv.yagamin.net", "/", hours_ago=40 * 24)
    fresh = _access_entry("tv.yagamin.net", "/", hours_ago=1)
    deleted = prune_access_log(retention_days=35)
    assert deleted == 1
    remaining = set(AccessLogEntry.objects.values_list("pk", flat=True))
    assert old.pk not in remaining
    assert fresh.pk in remaining


def test_access_stats_api_requires_staff(http_client, db):
    res = http_client.get("/api/v1/admin/access-stats")
    assert res.status_code == 401


def test_access_stats_api_staff(staff_client, db):
    _access_entry("tv.yagamin.net", "/", is_page=True)
    _access_entry("tv.yagamin.net", "/api/v1/home", is_page=False)
    res = staff_client.get("/api/v1/admin/access-stats", {"host": "tv.yagamin.net"})
    assert res.status_code == 200
    body = res.json()
    assert body["host"] == "tv.yagamin.net"
    assert "tv.yagamin.net" in body["hosts"]
    assert body["total_hits"] >= 2
