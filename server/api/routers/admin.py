# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d) 向け JSON API。staff 限定 (staff_auth)。

公開フロントの島と同じく既存 view 層のロジックを薄く JSON 化する。最初の縦スライスは
読み取り専用の members 統計 + 権利ダッシュボード (CRUD/タイムラインは後続スライス)。
Router 全体に staff_auth を着せる (非 staff/会員/匿名は 401)。
"""

from __future__ import annotations

import datetime
from collections import Counter

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from api.auth import staff_auth
from api.schemas import MemberStatsOut, RightsDashboardOut

router = Router(tags=["admin"], auth=staff_auth)

# 年齢層バケット (members.staff_views と揃える)
_AGE_BUCKETS = [
    (0, 9, "〜9歳"),
    (10, 19, "10代"),
    (20, 29, "20代"),
    (30, 39, "30代"),
    (40, 49, "40代"),
    (50, 59, "50代"),
    (60, 69, "60代"),
    (70, 200, "70代〜"),
]


@router.get("/admin/members/stats", response=MemberStatsOut)
def members_stats(request: HttpRequest):
    """会員統計 (staff_views.stats と同じ集計)。個人は特定せず集計値のみ。"""
    from members.models import Gender, Member, TwoFactorMethod
    from members.stats import split_by_country

    rows = list(
        Member.objects.values(
            "birth_year",
            "gender",
            "postal_code",
            "country",
            "email_verified_at",
            "totp_confirmed_at",
            "two_factor_method",
        )
    )
    total = len(rows)
    this_year = datetime.date.today().year
    verified = sum(1 for r in rows if r["email_verified_at"] or r["totp_confirmed_at"])

    gender_counts = Counter(r["gender"] for r in rows)
    tfa_counts = Counter(r["two_factor_method"] for r in rows)
    age_counts: Counter = Counter()
    for r in rows:
        age = this_year - (r["birth_year"] or this_year)
        for lo, hi, label in _AGE_BUCKETS:
            if lo <= age <= hi:
                age_counts[label] += 1
                break
    jp_rows, overseas_count, country_rows = split_by_country(rows)
    region_counts = Counter((r["postal_code"] or "")[:3] for r in jp_rows if r["postal_code"])

    def _rows(pairs):
        return [{"label": label, "count": count} for label, count in pairs]

    return {
        "total": total,
        "verified": verified,
        "unverified": total - verified,
        "gender": _rows((label, gender_counts.get(val, 0)) for val, label in Gender.choices),
        "tfa": _rows((label, tfa_counts.get(val, 0)) for val, label in TwoFactorMethod.choices),
        "age": _rows((label, age_counts.get(label, 0)) for _, _, label in _AGE_BUCKETS),
        "country": _rows(country_rows),
        "region": _rows(sorted(region_counts.items(), key=lambda kv: -kv[1])[:15]),
        # region は JP のみが母数。除外数を出さないと「会員が減った」ように見える。
        "region_overseas_excluded": overseas_count,
    }


@router.get("/admin/rights/dashboard", response=RightsDashboardOut)
def rights_dashboard(request: HttpRequest):
    """権利ダッシュボード (rights.views.dashboard と同じ): 期限切れ間近の配信権。"""
    from datetime import timedelta

    from rights.models import DistributionRight

    now = timezone.now()
    expiring = []
    qs = (
        DistributionRight.objects.filter(
            allow_vod=True,
            available_until__isnull=False,
            available_until__gte=now,
            available_until__lte=now + timedelta(days=30),
        )
        .select_related("program", "program__channel")
        .order_by("available_until")
    )
    for r in qs:
        lt = timezone.localtime(r.available_until)
        expiring.append(
            {
                "program_id": r.program_id,
                "program_title": r.program.title,
                "channel": r.program.channel.name,
                "holder": r.holder,
                "available_until": f"{lt.year}/{lt.month}/{lt.day}",
            }
        )

    return {"expiring": expiring}
