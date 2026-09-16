# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""有料会員ゲート。member_login_required と同型 (公開 urlconf のリテラルパス)。"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.shortcuts import redirect

from subscriptions.services import has_active_subscription


def subscription_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        member = getattr(request, "member", None)
        if not member:
            qs = urlencode({"next": request.get_full_path()})
            return redirect(f"/members/login/?{qs}")
        if not has_active_subscription(member):
            return redirect("/subscriptions/")  # 未加入は加入ページへ
        return view(request, *args, **kwargs)

    return _wrapped
