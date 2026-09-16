# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""簡易おすすめ (#PERS-03)。視聴履歴 + マイリストの「よく見るジャンル」から、

見逃し再生できる未視聴の番組をルールベースで薦める。ML は使わない (数人規模で十分)。
履歴が無ければ空 → 呼び出し側でセクションごと出さない (degrade)。
"""

from __future__ import annotations

from collections import Counter

from django.db.models import Q

from members.models import Favorite, WatchHistory
from scheduling import vod as vod_mod
from scheduling.models import Program


def preferred_genres(member, top: int = 3) -> list[str]:
    """会員の視聴履歴 + マイリスト番組の resolved_genre を集計し、上位ジャンルを返す。"""
    watched_ids = WatchHistory.objects.filter(member=member).values("program_id")
    fav_ids = Favorite.objects.filter(member=member).values("program_id")
    progs = Program.objects.filter(Q(id__in=watched_ids) | Q(id__in=fav_ids)).select_related(
        "series"
    )
    counter: Counter[str] = Counter()
    for p in progs:
        g = p.resolved_genre
        if g:
            counter[g] += 1
    return [g for g, _ in counter.most_common(top)]


def recommended_vod(member, *, limit: int = 8) -> list[Program]:
    """よく見るジャンルの「見逃し再生できる未視聴」番組 (新しい順)。履歴が無ければ空。"""
    if not member:  # 未ログイン (request.member は None を包む SimpleLazyObject のことがある)
        return []
    genres = preferred_genres(member)
    if not genres:
        return []
    watched_ids = set(
        WatchHistory.objects.filter(member=member).values_list("program_id", flat=True)
    )
    qs = (
        vod_mod.available_vod_qs()
        .filter(Q(genre__in=genres) | (Q(genre="") & Q(series__genre__in=genres)))
        .exclude(id__in=watched_ids)
    )
    return list(qs[:limit])
