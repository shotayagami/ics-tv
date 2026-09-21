# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""request.creator_account を session から遅延復元する (members.middleware と同型)。

SessionMiddleware の後・MemberAuthMiddleware の直後に置く。専用URLは creator.* ホストにしか
存在しないため、他ホストで立っても無害 (参照しなければ DB を引かない SimpleLazyObject)。
"""

from __future__ import annotations

from django.utils.functional import SimpleLazyObject

from fanclub.creator_auth import SESSION_KEY


def _load_creator_account(request):
    cid = request.session.get(SESSION_KEY)
    if not cid:
        return None
    from fanclub.models import CreatorAccount

    return CreatorAccount.objects.filter(pk=cid, is_active=True).select_related("creator").first()


class CreatorAccountAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.creator_account = SimpleLazyObject(lambda: _load_creator_account(request))
        return self.get_response(request)
