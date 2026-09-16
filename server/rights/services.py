# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配信権による VOD ゲート (#RIGHTS-02)。

ルール: 番組に DistributionRight が 1 件でも在る場合は権利が支配する。
有効な VOD 許諾 (allow_vod かつ期間内) が無ければ VOD から外す。権利レコードが無い番組は
権利上の制約なしとみなし編成側設定のみで判断する (全番組への権利登録を強制しない)。
"""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone


def _active_vod_window(now):
    return Q(available_from__isnull=True) | Q(available_from__lte=now), (
        Q(available_until__isnull=True) | Q(available_until__gt=now)
    )


def filter_vod_allowed(qs, now=None):
    """配信権で VOD 不可の番組を除外した Program queryset を返す。"""
    from rights.models import DistributionRight

    now = now or timezone.now()
    any_right = DistributionRight.objects.filter(program=OuterRef("pk"))
    from_ok, until_ok = _active_vod_window(now)
    active_vod = (
        DistributionRight.objects.filter(program=OuterRef("pk"), allow_vod=True)
        .filter(from_ok)
        .filter(until_ok)
    )
    return qs.annotate(
        _has_right=Exists(any_right),
        _has_active_vod=Exists(active_vod),
    ).exclude(_has_right=True, _has_active_vod=False)


def vod_allowed(program, now=None) -> bool:
    """単一番組の VOD 可否 (権利視点)。権利レコードが無ければ True。"""
    now = now or timezone.now()
    rights = list(program.distribution_rights.all())
    if not rights:
        return True
    return any(r.vod_active(now) for r in rights)
