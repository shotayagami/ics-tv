# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員のセッションログイン状態 (Django User 非依存)。

delivery/auth.py は `auth_login(User)` を使うが、会員は User と分離するため session に
member_id を直接持つ。ログイン成立で `cycle_key()` (セッション固定化対策)、ログアウトで
`flush()`。第2要素が必要なログインは pending 状態 (PENDING_*) を経由し、第2要素通過まで
SESSION_KEY を立てない (= 中途半端な状態ではログイン扱いにしない)。
"""

from __future__ import annotations

SESSION_KEY = "member_id"
PENDING_ID_KEY = "member_2fa_pending_id"
PENDING_METHOD_KEY = "member_2fa_method"


def login_member(request, member) -> None:
    """会員ログインを確立する (第2要素まで終わった後に呼ぶ)。"""
    clear_pending_2fa(request)
    request.session[SESSION_KEY] = member.pk
    request.session.cycle_key()  # セッション固定化対策 (データは保持される)
    request.member = member


def logout_member(request) -> None:
    request.session.flush()
    request.member = None


def get_member_id(request):
    return request.session.get(SESSION_KEY)


# --- 第2要素 (2FA) の保留状態 ---
def start_pending_2fa(request, member, method: str) -> None:
    request.session[PENDING_ID_KEY] = member.pk
    request.session[PENDING_METHOD_KEY] = method


def get_pending_member(request):
    mid = request.session.get(PENDING_ID_KEY)
    if not mid:
        return None
    from members.models import Member

    return Member.objects.filter(pk=mid, is_active=True).first()


def get_pending_method(request):
    return request.session.get(PENDING_METHOD_KEY)


def clear_pending_2fa(request) -> None:
    request.session.pop(PENDING_ID_KEY, None)
    request.session.pop(PENDING_METHOD_KEY, None)
