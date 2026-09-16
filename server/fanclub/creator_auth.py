# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* ホストのクリエイター本人セッションログイン状態 (members/auth.py と同型)。

CreatorAccount は Django User にも Member にも紐付かない第4の主体。session に
creator_account_id を直接持つ (JWT/別トークン層は持たない)。
"""

from __future__ import annotations

SESSION_KEY = "creator_account_id"
PENDING_INVITE_TOKEN_KEY = "creator_pending_invite_token"


def login_creator_account(request, account) -> None:
    """クリエイターログインを確立する。"""
    request.session[SESSION_KEY] = account.pk
    request.session.cycle_key()  # セッション固定化対策 (データは保持される)
    request.creator_account = account


def logout_creator_account(request) -> None:
    request.session.flush()
    request.creator_account = None


def get_creator_account_id(request):
    return request.session.get(SESSION_KEY)
