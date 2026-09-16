# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""エンタイトルメント解決。会員 → 有効サブスク → Plan → 特典集合。

公開テンプレ (context_processor)・ゲートデコレータ・コメントバッジ等から使う。未ログイン/無料は空集合。
"""

from __future__ import annotations

from subscriptions.models import MemberSubscription


def get_subscription(member):
    """会員の MemberSubscription (plan 込み) を1件返す。無ければ None。"""
    if not member:
        return None
    return MemberSubscription.objects.filter(member=member).select_related("plan").first()


def has_active_subscription(member) -> bool:
    sub = get_subscription(member)
    return bool(sub and sub.is_active)


def entitlements(member) -> set[str]:
    """有効サブスクの Plan が付与する特典集合。無効/無料/未ログインは空集合。"""
    sub = get_subscription(member)
    if sub and sub.is_active and sub.plan:
        return sub.plan.features
    return set()


def subscriber_member_ids(member_ids) -> set[int]:
    """与えた member_id のうち有効サブスクを持つ集合 (N+1 回避用)。"""
    ids = [m for m in member_ids if m]
    if not ids:
        return set()
    subs = MemberSubscription.objects.filter(member_id__in=ids).only(
        "member_id", "status", "current_period_end"
    )
    return {s.member_id for s in subs if s.is_active}


def comment_perk_member_ids(member_ids) -> set[int]:
    """与えた member_id のうち comment_perk 特典 (会員バッジ) を持つ集合。

    バッジは「有効サブスク」一般ではなく「コメント特典付きプラン」に紐付ける (特典フラグを実効化)。
    N+1 回避のため 1 クエリで plan ごと引く。
    """
    ids = [m for m in member_ids if m]
    if not ids:
        return set()
    subs = MemberSubscription.objects.filter(member_id__in=ids).select_related("plan")
    return {s.member_id for s in subs if s.is_active and s.plan and s.plan.feat_comment_perk}
