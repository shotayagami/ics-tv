# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開テンプレに会員の特典 (entitlements) と加入状況 (is_subscriber) を供給。

request.member (members.middleware) があれば 1 クエリでサブスクを引く。未ログインは即空。
"""

from __future__ import annotations

from subscriptions.services import get_subscription


def entitlements(request) -> dict:
    member = getattr(request, "member", None)
    sub = get_subscription(member)
    active = bool(sub and sub.is_active)
    feats = sub.plan.features if (active and sub.plan) else set()
    return {"entitlements": feats, "is_subscriber": active}
