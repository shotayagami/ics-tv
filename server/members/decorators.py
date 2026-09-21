# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員向けアクセス制御デコレータ。

公開 urlconf はリテラルパスで reverse 名前空間に依存しない (config/urls_public.py の方針)。
`settings.LOGIN_URL` は納品ポータル (/delivery/login/) を指すので使わず、会員ログインの
リテラルパスへリダイレクトする。

- member_login_required: 未ログインは /members/login/?next=... へ。
- member_verified_required: 本人確認 (メール or TOTP) 未完了は /members/verify-required/ へ。
  これが「コードを受け取れない未認証ユーザに機能制限をかける土台」。今は具体的な機能には
  まだ着けない (コメント機能等が将来このデコレータを着る)。
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.shortcuts import redirect

LOGIN_PATH = "/members/login/"
VERIFY_REQUIRED_PATH = "/members/verify-required/"


def member_login_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not getattr(request, "member", None):
            qs = urlencode({"next": request.get_full_path()})
            return redirect(f"{LOGIN_PATH}?{qs}")
        return view(request, *args, **kwargs)

    return _wrapped


def member_verified_required(view):
    @wraps(view)
    @member_login_required
    def _wrapped(request, *args, **kwargs):
        if not request.member.is_verified:
            return redirect(VERIFY_REQUIRED_PATH)
        return view(request, *args, **kwargs)

    return _wrapped
