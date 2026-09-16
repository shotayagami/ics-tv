# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* ホスト向けアクセス制御デコレータ (members/decorators.py と同型)。

creator.* はホスト丸ごとクリエイター専用のため、公開urlconfの `/members/` のような
プレフィックスは付けない (ログインパスはホスト直下)。
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.shortcuts import redirect

LOGIN_PATH = "/login/"


def creator_login_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not getattr(request, "creator_account", None):
            qs = urlencode({"next": request.get_full_path()})
            return redirect(f"{LOGIN_PATH}?{qs}")
        return view(request, *args, **kwargs)

    return _wrapped
