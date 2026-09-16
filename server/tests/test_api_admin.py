# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d) の admin API: members 統計 + rights ダッシュボード。

既定ホスト testserver は ICSTV_ADMIN_HOSTS に含まれ管理 urlconf。staff_auth (SessionAuthIsStaff)
で staff 限定 = 匿名/非 staff は 401、staff は 200。集計値・期限切れ間近一覧を検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from members.models import Member


def _member(email, *, birth_year=1990, gender="", postal="1000001", country="JP"):
    m = Member(
        email=email,
        nickname="x",
        birth_year=birth_year,
        birth_month=4,
        postal_code=postal,
        gender=gender,
        country=country,
    )
    m.email_verified_at = timezone.now()
    m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
    m.save()
    return m


# ---- 認可 ----


def test_members_stats_requires_auth(http_client, db):
    assert http_client.get("/api/v1/admin/members/stats").status_code == 401


def test_members_stats_rejects_non_staff(db, django_user_model):
    from django.test import Client

    u = django_user_model.objects.create_user(
        username="plain", password="x", is_staff=False
    )  # pragma: allowlist secret - test only
    c = Client()
    c.force_login(u)
    assert c.get("/api/v1/admin/members/stats").status_code == 401


def test_rights_dashboard_requires_auth(http_client, db):
    assert http_client.get("/api/v1/admin/rights/dashboard").status_code == 401


# ---- members 統計 ----


def test_members_stats_aggregates(staff_client, db):
    _member("a@e.com", birth_year=1990)  # 30代 (this_year-1990)
    _member("b@e.com", birth_year=1990)
    _member("c@e.com", birth_year=2010)  # 10代
    d = staff_client.get("/api/v1/admin/members/stats").json()
    assert d["total"] == 3
    assert d["verified"] == 3 and d["unverified"] == 0
    age = {r["label"]: r["count"] for r in d["age"]}
    assert age["30代"] == 2 and age["10代"] == 1
    # 集計テーブルは全バケットを返す (0 件も label を出す)
    assert {"label", "count"} <= set(d["gender"][0].keys())
    assert any(r["label"].startswith("100") for r in d["region"])  # 郵便番号 上3桁


def test_members_stats_empty(staff_client, db):
    d = staff_client.get("/api/v1/admin/members/stats").json()
    assert d["total"] == 0 and d["region"] == []
    assert d["country"] == [] and d["region_overseas_excluded"] == 0


def test_members_stats_region_excludes_overseas(staff_client, db):
    """地域内訳は日本の郵便番号体系に依存するため、海外在住は母数から外して件数で示す。"""
    _member("jp@e.com")
    _member("gb@e.com", country="GB", postal="SW1A 1AA")
    d = staff_client.get("/api/v1/admin/members/stats").json()
    assert d["total"] == 2
    assert [r["label"] for r in d["region"]] == ["100"]  # 海外の "SW1" は棚に入らない
    assert d["region_overseas_excluded"] == 1
    # 国内訳は表示名で返す (SPA 側でコード→名称の対応表を持たせない)
    assert {r["label"]: r["count"] for r in d["country"]} == {"日本": 1, "イギリス": 1}


# ---- rights ダッシュボード ----


def test_rights_dashboard_ok(staff_client, channel, asset_ready, db):
    d = staff_client.get("/api/v1/admin/rights/dashboard").json()
    assert set(d) == {"expiring"}


def test_rights_dashboard_lists_expiring(staff_client, channel, asset_ready, db):
    from rights.models import DistributionRight
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    p = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="期限間近番組",
        start_at=now - timedelta(hours=2),
        end_at=now - timedelta(hours=1),
        asset=asset_ready,
    )
    DistributionRight.objects.create(
        program=p, holder="権利者A", allow_vod=True, available_until=now + timedelta(days=10)
    )
    d = staff_client.get("/api/v1/admin/rights/dashboard").json()
    assert any(
        e["program_title"] == "期限間近番組" and e["holder"] == "権利者A" for e in d["expiring"]
    )
