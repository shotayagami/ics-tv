# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ファンクラブ エンタイトルメント解決 + 加入/退会。

member → CreatorMembership → tier.level を解決する。subscriptions.services と同じ思想:
未加入/未ログインは None/空。creator が status=suspended のときはフェイルクローズ (視聴不可)。

依存方向: fanclub は scheduling を import しない。creator_for_series などは series オブジェクトを
duck-typing (series.pk のみ参照) で受け取ることで、scheduling → fanclub の import 方向のみを成立させる
(scheduling.vod が本モジュールを呼ぶ)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from fanclub.models import (
    Creator,
    CreatorAddonGrant,
    CreatorMembership,
    CreatorSeriesLink,
    CreatorStatus,
    CreatorTier,
    MembershipStatus,
)


def tier_is_joinable(tier: CreatorTier) -> bool:
    """このティアに新規加入(課金)できるか。

    level0(無料)は常時可。有料ティアは Stripe Price(stripe_price_id) が設定され、かつ
    creator の Stripe Connect オンボーディングが完了(stripe_connect_onboarded)して初めて
    解禁される(#27 Phase B)。どちらか一方でも欠けていれば「準備中」として弾く
    (subscriptions.ENFORCED_ENTITLEMENTS と同じ「今動くものだけ許可」思想)。
    """
    if tier.level == 0:
        return True
    return bool(tier.stripe_price_id) and tier.creator.stripe_connect_onboarded


class FanclubError(Exception):
    """fanclub サービス層の業務エラー基底。"""


class TierNotJoinableError(FanclubError):
    """指定ティアは現在加入できない (未開設 / Phase A では有料ティアが準備中)。"""


class TierChangeNotAllowedError(FanclubError):
    """有料ティア間の変更として成立しない組み合わせ (無料からの移行・同一ティア・別creator 等)。"""


def has_open_paid_membership(member) -> bool:
    """Stripe の購読に紐付く有効な在籍があるか (課金が続きうる)。期末解約の予約済みは含めない。"""
    return (
        CreatorMembership.objects.filter(
            member=member, status=MembershipStatus.ACTIVE, cancel_at_period_end=False
        )
        .exclude(stripe_subscription_id="")
        .exists()
    )


def member_level(member, creator: Creator | None) -> int | None:
    """member の creator における実効ティア level。未加入/未ログインは None。

    Stripe 購読の在籍 (CreatorMembership) と、追加側アプリが付与した期限付き格上げ
    (CreatorAddonGrant) の高い方を採る。どちらか一方の失効が他方を巻き込まない (docstring 参照)。
    """
    if not member or creator is None:
        return None
    levels = []
    m = (
        CreatorMembership.objects.filter(
            member=member, creator=creator, status=MembershipStatus.ACTIVE
        )
        .select_related("tier")
        .first()
    )
    if m is not None:
        levels.append(m.tier.level)
    grant = (
        CreatorAddonGrant.objects.filter(
            member=member, creator=creator, expires_at__gt=timezone.now()
        )
        .select_related("tier")
        .order_by("-tier__level")
        .first()
    )
    if grant is not None:
        levels.append(grant.tier.level)
    return max(levels) if levels else None


def can_view_level(required_level: int | None, member, creator: Creator | None) -> bool:
    """required_level (NULL=完全公開) を member が満たすか。creator suspended/未紐付は視聴不可 (fail-closed)。"""
    if required_level is None:
        return True
    if creator is None or creator.status != CreatorStatus.ACTIVE:
        return False
    lvl = member_level(member, creator)
    return lvl is not None and lvl >= required_level


def creator_for_series(series) -> Creator | None:
    """series (scheduling.Series インスタンス、None可) に紐づく Creator。未紐付けは None。"""
    if series is None:
        return None
    link = CreatorSeriesLink.objects.filter(series_id=series.pk).select_related("creator").first()
    return link.creator if link else None


def member_levels_by_series(member, series_ids) -> dict[int, int | None]:
    """複数 series をまたぐ一覧向けバルク解決 (N+1回避)。

    1) series→creator を1クエリ、2) member→creator ごとの level を1クエリで解決し、
    series数に関わらず高々2クエリで済ませる (subscriptions.services.subscriber_member_ids と同じ思想)。
    creator 未紐付けの series はマップに含まれない (呼び出し側は .get(sid) で None 扱い = 完全公開扱い)。
    """
    ids = [s for s in series_ids if s]
    if not ids:
        return {}
    creator_by_series = dict(
        CreatorSeriesLink.objects.filter(series_id__in=ids).values_list("series_id", "creator_id")
    )
    if not member or not creator_by_series:
        return dict.fromkeys(creator_by_series, None)
    creator_ids = set(creator_by_series.values())
    rows = CreatorMembership.objects.filter(
        member=member, creator_id__in=creator_ids, status=MembershipStatus.ACTIVE
    ).values("creator_id", "tier__level")
    level_by_creator = {r["creator_id"]: r["tier__level"] for r in rows}
    return {sid: level_by_creator.get(cid) for sid, cid in creator_by_series.items()}


def fc_gate_reason(required_level: int | None, member, creator: Creator | None) -> str:
    """'' / 'login' / 'fc_join' / 'fc_unavailable'。vod.py / core.views / series_public.py 共通語彙。"""
    if required_level is None or can_view_level(required_level, member, creator):
        return ""
    if creator is None or creator.status != CreatorStatus.ACTIVE:
        return "fc_unavailable"
    tier = CreatorTier.objects.filter(creator=creator, level=required_level, is_active=True).first()
    if tier is None or not tier_is_joinable(tier):
        return "fc_unavailable"  # 未開設 or Stripe未整備(準備中)
    return "login" if not member else "fc_join"


def ensure_free_tier(creator: Creator) -> CreatorTier:
    """level0(無料)ティアが無ければ作る (Creator 新規作成時に呼ぶ)。"""
    tier, _ = CreatorTier.objects.get_or_create(
        creator=creator,
        level=0,
        defaults={"name": "無料会員", "price_minor": None, "is_active": True},
    )
    return tier


def ensure_member_no(membership: CreatorMembership) -> int:
    """会員番号 (creator 単位の連番) を未採番なら採番する。既採番ならそのまま返す。

    採番は creator 行のロックで直列化する。既存の membership 行だけを select_for_update しても
    同時に走る別トランザクションの INSERT は防げず同番になりうるため (ファントム)、
    「その creator への採番」という単一の資源を creator 行で表現している。
    退会後の再加入は行を再利用するため番号も引き継がれる。
    """
    if membership.member_no:
        return membership.member_no
    with transaction.atomic():
        # ロック取得が目的なので結果は使わない。
        list(Creator.objects.select_for_update().filter(pk=membership.creator_id))
        latest = CreatorMembership.objects.filter(creator_id=membership.creator_id).aggregate(
            mx=Max("member_no")
        )["mx"]
        membership.member_no = (latest or 0) + 1
        membership.save(update_fields=["member_no"])
    return membership.member_no


def join(member, creator: Creator, tier: CreatorTier) -> CreatorMembership:
    """member を creator の無料ティア(level0)へ加入させる。

    有料ティアはこの経路では加入できない(Stripe Checkout + Webhook のみが加入させる。
    fanclub.webhook / fanclub.checkout 参照)。誤って有料ティアを渡すと即座に拒否する。
    """
    if tier.creator_id != creator.id or not tier.is_active:
        raise TierNotJoinableError("このプランは現在受け付けていません。")
    if tier.level > 0:
        raise TierNotJoinableError("有料プランは決済ページからお申し込みください。")
    now = timezone.now()
    membership, created = CreatorMembership.objects.get_or_create(
        member=member,
        creator=creator,
        defaults={"tier": tier, "status": MembershipStatus.ACTIVE, "joined_at": now},
    )
    if not created and membership.status != MembershipStatus.ACTIVE:
        membership.tier = tier
        membership.status = MembershipStatus.ACTIVE
        membership.joined_at = now
        membership.left_at = None
        membership.save(update_fields=["tier", "status", "joined_at", "left_at"])
    ensure_member_no(membership)
    return membership


def join_free_tier(member, creator: Creator) -> CreatorMembership:
    """公開サイトの「無料で参加する」ボタン用ショートカット。"""
    tier = CreatorTier.objects.filter(creator=creator, level=0, is_active=True).first()
    if tier is None:
        raise TierNotJoinableError("この番組はまだファンクラブを開設していません。")
    return join(member, creator, tier)


def active_paid_membership(member, creator: Creator) -> CreatorMembership | None:
    """member の creator における「Stripe サブスクを伴う有料在籍」。無い/無料在籍なら None。

    ティア変更が成立するのはこの状態に限る (無料 level0 から有料へは Checkout の新規加入経路、
    未加入からも同様)。
    """
    if not member or creator is None:
        return None
    m = (
        CreatorMembership.objects.filter(
            member=member, creator=creator, status=MembershipStatus.ACTIVE
        )
        .select_related("tier")
        .first()
    )
    if m is None or m.tier.level == 0 or not m.stripe_subscription_id:
        return None
    return m


def tier_rows(member, creator: Creator | None) -> tuple[CreatorMembership | None, list[dict]]:
    """公開画面のティアカード行 (加入/変更導線)。(my_membership, [{tier, action}]) を返す。

    action: 'join' (未加入/無料在籍 → 新規 Checkout) / 'current' / 'upgrade' / 'downgrade'。
    有料在籍中は上下どちらへも変更できるため「現レベル以下を隠す」ことはしない (#27 §3.1)。
    シリーズ詳細とクリエイターページ (/fc/<slug>/) で共用。
    """
    if creator is None:
        return None, []
    my_membership = active_paid_membership(member, creator)
    rows = []
    for t in creator.tiers.filter(is_active=True, level__gt=0).order_by("level"):
        if not tier_is_joinable(t):
            continue
        if my_membership is None:
            action = "join"
        elif t.level == my_membership.tier.level:
            action = "current"
        else:
            action = "upgrade" if t.level > my_membership.tier.level else "downgrade"
        rows.append({"tier": t, "action": action})
    return my_membership, rows


def safe_theme_color(creator: Creator) -> str:
    """テンプレートの style へ素通しできる theme_color。形式不一致は '' (既定アクセント)。

    モデルの RegexValidator は full_clean 経由でしか走らないため、描画側でも再検証して
    CSS インジェクションを二重に塞ぐ。
    """
    import re

    color = creator.theme_color or ""
    return color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else ""


def plan_tier_change(membership: CreatorMembership, new_tier: CreatorTier) -> str:
    """有料ティア間の変更方向を判定する。'upgrade' / 'downgrade'。

    ティアは累積型 (F5) で level の昇順が特典の包含順序と一致するため、level 比較で方向が決まる
    (price_minor は同額の並列ティアもあり得るので判定には使わない)。成立しない組み合わせは例外。
    """
    if new_tier.creator_id != membership.creator_id:
        raise TierChangeNotAllowedError("このプランは別のファンクラブのものです。")
    if not new_tier.is_active or new_tier.level == 0:
        raise TierChangeNotAllowedError("このプランへは変更できません。")
    if not tier_is_joinable(new_tier):
        raise TierNotJoinableError("このプランはまだ準備中です。")
    if new_tier.level == membership.tier.level:
        raise TierChangeNotAllowedError("すでにこのプランをご利用中です。")
    return "upgrade" if new_tier.level > membership.tier.level else "downgrade"


def change_tier(membership: CreatorMembership, new_tier: CreatorTier):
    """有料ティア間の変更を実行する。戻り値は (direction, 適用予定時刻 or None)。

    アップグレードは即時 + 日割り請求、ダウングレードは期末適用 (F5)。Stripe 呼び出しは
    fanclub.stripe_gateway 側。gateway が例外を投げた場合は DB を一切書かずに伝播させる
    (Stripe とDB が食い違った状態を作らない)。

    「Stripe 系フィールドは Webhook のみが更新する」規律の例外として、アップグレード成功時は
    ここで tier を進める。modify() が成功した時点で Stripe 側の Price は確定しており推測ではない
    ためで (_sync_from_checkout が metadata から tier を確定させるのと同じ扱い)、後続の
    customer.subscription.updated は同じ値を冪等に再確認するだけになる。
    """
    from fanclub import stripe_gateway as gw

    direction = plan_tier_change(membership, new_tier)
    if direction == "upgrade":
        gw.change_subscription_price_now(
            subscription_id=membership.stripe_subscription_id,
            new_price_id=new_tier.stripe_price_id,
        )
        membership.tier = new_tier
        # 上位へ移ったので、残っていた下位への予約は意味を失う (Stripe 側も modify で
        # スケジュールから release される)。
        membership.pending_tier = None
        membership.pending_tier_effective_at = None
        membership.save(update_fields=["tier", "pending_tier", "pending_tier_effective_at"])
        return direction, None

    effective_epoch = gw.schedule_subscription_price_at_period_end(
        subscription_id=membership.stripe_subscription_id,
        new_price_id=new_tier.stripe_price_id,
    )
    effective_at = datetime.fromtimestamp(effective_epoch, tz=UTC)
    membership.pending_tier = new_tier
    membership.pending_tier_effective_at = effective_at
    membership.save(update_fields=["pending_tier", "pending_tier_effective_at"])
    return direction, effective_at


def cancel_tier_change(membership: CreatorMembership) -> bool:
    """ダウングレード予約を取り消す。予約が無ければ False (べき等)。"""
    from fanclub import stripe_gateway as gw

    if membership.pending_tier_id is None:
        return False
    gw.release_subscription_schedule(subscription_id=membership.stripe_subscription_id)
    membership.pending_tier = None
    membership.pending_tier_effective_at = None
    membership.save(update_fields=["pending_tier", "pending_tier_effective_at"])
    return True


def can_use_chat(member, creator: Creator | None) -> bool:
    """会員限定チャットの読み書き資格 (#27 §3.1 Should)。

    creator.chat_required_level (NULL=無効 / 0=無料会員以上 / n=有料ティアn以上) ×
    在籍ティア。読む資格と書く資格は分けない (Fanicon グルチャ型)。
    """
    if creator is None or creator.chat_required_level is None:
        return False
    return can_view_level(creator.chat_required_level, member, creator)


def leave(member, creator: Creator) -> None:
    """在籍中なら退会させる (べき等: 非在籍への呼び出しは no-op)。

    gift_expires_at は追加提供側機能の名残 (models.CreatorMembership を参照)。移行由来の
    残余値が残らないよう、退会時は防御的にクリアする。
    """
    CreatorMembership.objects.filter(
        member=member, creator=creator, status=MembershipStatus.ACTIVE
    ).update(status=MembershipStatus.LEFT, left_at=timezone.now(), gift_expires_at=None)
