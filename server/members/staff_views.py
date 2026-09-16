# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員統計ダッシュボード (staff 専用・管理ホスト)。

収集した生年月・性別・郵便番号を当初の目的どおり統計に流用する集計画面。個人は特定せず
集計値のみ表示。会員数は homelab 規模なので 1 クエリで取得し Python で集計する。
"""

from __future__ import annotations

import datetime
from collections import Counter

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from members.models import Gender, Member, TwoFactorMethod
from members.stats import split_by_country

# 年齢層バケット (lo<=age<=hi)
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


@staff_member_required
def stats(request):
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

    ctx = {
        "active": "members",
        "total": total,
        "verified": verified,
        "unverified": total - verified,
        "gender_rows": [(label, gender_counts.get(val, 0)) for val, label in Gender.choices],
        "tfa_rows": [(label, tfa_counts.get(val, 0)) for val, label in TwoFactorMethod.choices],
        "age_rows": [(label, age_counts.get(label, 0)) for _, _, label in _AGE_BUCKETS],
        "region_rows": sorted(region_counts.items(), key=lambda kv: -kv[1])[:15],
        # region_rows は JP のみが母数。除外数を出さないと「会員が減った」ように見える。
        "region_overseas_excluded": overseas_count,
        "country_rows": country_rows,
    }
    return render(request, "members/stats.html", ctx)
