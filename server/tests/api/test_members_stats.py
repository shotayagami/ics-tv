# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員統計ダッシュボード (staff 専用・管理ホスト)。

管理ホスト (= 既定 testserver) で動くので override は不要。staff_client は conftest の
force_login 済み staff。年齢層は現在年からの相対で作り、年に依存しない検証にする。
"""

from __future__ import annotations

import datetime

from django.utils import timezone

from members.models import Member

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _mk(email, birth_year, gender, postal, **over):
    m = Member(
        email=email,
        nickname="t",
        birth_year=birth_year,
        birth_month=1,
        gender=gender,
        postal_code=postal,
        **over,
    )
    m.set_password(_PW)
    m.save()
    return m


def test_stats_requires_staff(http_client, db):
    res = http_client.get("/members-admin/stats/")
    assert res.status_code == 302 and "/admin/login/" in res.url


def test_stats_renders_for_staff(staff_client, db):
    res = staff_client.get("/members-admin/stats/")
    assert res.status_code == 200
    assert "会員統計" in res.content.decode("utf-8")


def test_stats_aggregation(staff_client, db):
    y = datetime.date.today().year
    # 30代男性 (age 35, verified) / 40代女性 (age 45, unverified)
    _mk("a@example.com", y - 35, "male", "1000001", email_verified_at=timezone.now())
    _mk("b@example.com", y - 45, "female", "1500002")
    res = staff_client.get("/members-admin/stats/")
    ctx = res.context
    assert ctx["total"] == 2
    assert ctx["verified"] == 1 and ctx["unverified"] == 1
    gd = dict(ctx["gender_rows"])
    assert gd["男"] == 1 and gd["女"] == 1 and gd["無回答"] == 0
    ad = dict(ctx["age_rows"])
    assert ad["30代"] == 1 and ad["40代"] == 1
    rd = dict(ctx["region_rows"])
    assert rd.get("100") == 1 and rd.get("150") == 1
