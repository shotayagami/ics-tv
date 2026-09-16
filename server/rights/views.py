# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""権利・コンプライアンス staff 画面 (#RIGHTS-02)。

配信権の期限切れ間近の一覧。管理ホストのみ (staff)。
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone

from rights.models import DistributionRight


@staff_member_required
def dashboard(request):
    """権利ダッシュボード: 配信権の期限切れ間近。"""
    now = timezone.now()
    expiring = (
        DistributionRight.objects.filter(
            allow_vod=True,
            available_until__isnull=False,
            available_until__gte=now,
            available_until__lte=now + timedelta(days=30),
        )
        .select_related("program", "program__channel")
        .order_by("available_until")
    )
    return render(request, "rights/dashboard.html", {"expiring": expiring})
