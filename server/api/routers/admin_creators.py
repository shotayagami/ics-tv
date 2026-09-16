# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ファンクラブ studio 管理 (#27)。Creator CRUD + Series紐付け + ティア + 招待 + 枠契約 + 会員サマリ。

`/admin/scheduling/{slug}/...` (AudienceForm 等) と異なり Creator はチャンネルに従属しない
グローバルなエンティティのため `/admin/fanclub/...` に独立して切る。
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.db import IntegrityError
from django.db.models import Sum
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Query, Router
from ninja.errors import HttpError

from api.auth import staff_auth
from api.schemas import (
    CreatorAdminOut,
    CreatorIn,
    CreatorInvitationIn,
    CreatorInvitationOut,
    CreatorMembersSummaryOut,
    CreatorRevenueSummaryOut,
    CreatorSeriesOut,
    CreatorTierIn,
    CreatorTierOut,
    FcSettlementSummaryOut,
    OkOut,
    SeriesCreatorLinkOut,
    SeriesOptionOut,
    SlotContractIn,
    SlotContractOut,
)
from fanclub.models import (
    Creator,
    CreatorInvitation,
    CreatorMembership,
    CreatorSeriesLink,
    CreatorTier,
    FcSettlement,
    MembershipStatus,
    SlotContract,
    SlotContractStatus,
)
from fanclub.services import tier_is_joinable

router = Router(tags=["admin"], auth=staff_auth)

_INVITATION_TTL_DAYS = 7
_SETTLEMENT_PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
#  Creator CRUD                                                               #
# --------------------------------------------------------------------------- #


def _creator_out(c: Creator) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "slug": c.slug,
        "description": c.description,
        "status": c.status,
        "created_at": c.created_at.isoformat(),
        "series_count": c.series_links.count(),
        "member_count": c.memberships.filter(status=MembershipStatus.ACTIVE).count(),
        "active_contract_count": c.slot_contracts.filter(status="active").count(),
        "onboarding_status": c.onboarding_status,
        "identity_verified_at": c.identity_verified_at.isoformat()
        if c.identity_verified_at
        else "",
        "legal_name": c.legal_name,
        "is_individual": c.is_individual,
        "representative_name": c.representative_name,
        "address": c.address,
        "phone": c.phone,
        "hide_contact_details": c.hide_contact_details,
        "contact_email": c.contact_email,
        "invoice_registration_number": c.invoice_registration_number,
    }


def _apply_creator_in(c: Creator, payload: CreatorIn) -> None:
    c.name = payload.name.strip()
    c.slug = payload.slug.strip()
    c.description = payload.description
    c.status = payload.status
    c.onboarding_status = payload.onboarding_status
    c.legal_name = payload.legal_name
    c.is_individual = payload.is_individual
    c.representative_name = payload.representative_name
    c.address = payload.address
    c.phone = payload.phone
    c.hide_contact_details = payload.hide_contact_details
    c.contact_email = payload.contact_email
    c.invoice_registration_number = payload.invoice_registration_number


@router.get("/admin/fanclub/creators", response=list[CreatorAdminOut])
def creators_list(request: HttpRequest):
    creators = Creator.objects.all().order_by("name")
    return [_creator_out(c) for c in creators]


@router.post("/admin/fanclub/creators", response=CreatorAdminOut)
def creator_create(request: HttpRequest, payload: CreatorIn):
    from fanclub.services import ensure_free_tier

    c = Creator()
    _apply_creator_in(c, payload)
    if not c.name:
        raise HttpError(422, "名前は必須です")
    if not c.slug:
        raise HttpError(422, "slug は必須です")
    try:
        c.save()
    except IntegrityError as exc:
        raise HttpError(422, "この slug は既に使われています") from exc
    ensure_free_tier(c)
    return _creator_out(c)


@router.get("/admin/fanclub/creators/{creator_id}", response=CreatorAdminOut)
def creator_detail(request: HttpRequest, creator_id: int):
    c = get_object_or_404(Creator, pk=creator_id)
    return _creator_out(c)


@router.post("/admin/fanclub/creators/{creator_id}/verify-identity", response=CreatorAdminOut)
def creator_verify_identity(request: HttpRequest, creator_id: int):
    """スタッフが本人確認(書類授受等オフライン)を完了させた記録として identity_verified_at を打つ。"""
    c = get_object_or_404(Creator, pk=creator_id)
    c.identity_verified_at = timezone.now()
    c.save(update_fields=["identity_verified_at"])
    return _creator_out(c)


@router.put("/admin/fanclub/creators/{creator_id}", response=CreatorAdminOut)
def creator_update(request: HttpRequest, creator_id: int, payload: CreatorIn):
    c = get_object_or_404(Creator, pk=creator_id)
    _apply_creator_in(c, payload)
    if not c.name:
        raise HttpError(422, "名前は必須です")
    if not c.slug:
        raise HttpError(422, "slug は必須です")
    try:
        c.save()
    except IntegrityError as exc:
        raise HttpError(422, "この slug は既に使われています") from exc
    return _creator_out(c)


# --------------------------------------------------------------------------- #
#  Series 紐付け                                                              #
# --------------------------------------------------------------------------- #


@router.get("/admin/fanclub/series-options", response=list[SeriesOptionOut])
def series_options(request: HttpRequest, q: str = Query("")):
    """Series 紐付け候補検索。他 creator に既リンク済みのものも名前を添えて表示。"""
    from scheduling.models import Series

    qs = Series.objects.select_related("channel").order_by("title")
    if q:
        qs = qs.filter(title__icontains=q)
    qs = qs[:50]
    linked = dict(
        CreatorSeriesLink.objects.filter(series_id__in=[s.id for s in qs]).values_list(
            "series_id", "creator__name"
        )
    )
    return [
        {
            "id": s.id,
            "title": s.title,
            "channel_name": s.channel.name,
            "linked_creator_name": linked.get(s.id, ""),
        }
        for s in qs
    ]


@router.get("/admin/fanclub/series/{series_id}/creator", response=SeriesCreatorLinkOut)
def series_creator_lookup(request: HttpRequest, series_id: int):
    """series → creator_id の逆引き (SeriesDetailPage の投稿タブが公開範囲セレクトを出すのに使う)。"""
    link = CreatorSeriesLink.objects.filter(series_id=series_id).first()
    return {"creator_id": link.creator_id if link else None}


@router.get("/admin/fanclub/creators/{creator_id}/series", response=list[CreatorSeriesOut])
def creator_series_list(request: HttpRequest, creator_id: int):
    creator = get_object_or_404(Creator, pk=creator_id)
    links = creator.series_links.select_related("series", "series__channel").order_by(
        "series__title"
    )
    return [
        {
            "series_id": link.series_id,
            "series_title": link.series.title,
            "channel_name": link.series.channel.name,
        }
        for link in links
    ]


@router.post("/admin/fanclub/creators/{creator_id}/series", response=CreatorSeriesOut)
def creator_series_link(request: HttpRequest, creator_id: int, series_id: int):
    from scheduling.models import Series

    creator = get_object_or_404(Creator, pk=creator_id)
    series = get_object_or_404(Series, pk=series_id)
    try:
        link = CreatorSeriesLink.objects.create(series=series, creator=creator)
    except IntegrityError as exc:
        raise HttpError(422, "この番組は既に別のクリエイターに紐付けられています") from exc
    return {
        "series_id": link.series_id,
        "series_title": series.title,
        "channel_name": series.channel.name,
    }


@router.delete("/admin/fanclub/creators/{creator_id}/series/{series_id}", response=OkOut)
def creator_series_unlink(request: HttpRequest, creator_id: int, series_id: int):
    link = get_object_or_404(CreatorSeriesLink, creator_id=creator_id, series_id=series_id)
    link.delete()
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  ティア                                                                     #
# --------------------------------------------------------------------------- #


def _tier_out(t: CreatorTier) -> dict:
    return {
        "id": t.id,
        "level": t.level,
        "name": t.name,
        "description": t.description,
        "price_minor": t.price_minor,
        "is_active": t.is_active,
        "stripe_price_id": t.stripe_price_id,
        "joinable": tier_is_joinable(t),
    }


@router.get("/admin/fanclub/creators/{creator_id}/tiers", response=list[CreatorTierOut])
def tiers_list(request: HttpRequest, creator_id: int):
    creator = get_object_or_404(Creator, pk=creator_id)
    return [_tier_out(t) for t in creator.tiers.order_by("level")]


@router.post("/admin/fanclub/creators/{creator_id}/tiers", response=CreatorTierOut)
def tier_create(request: HttpRequest, creator_id: int, payload: CreatorTierIn):
    creator = get_object_or_404(Creator, pk=creator_id)
    if payload.level == 0:
        raise HttpError(422, "level0(無料)は常設のため新規作成できません")
    if payload.price_minor is None:
        raise HttpError(422, "有料ティアには price_minor が必要です")
    tier = CreatorTier(creator=creator, **payload.dict())
    try:
        tier.save()
    except IntegrityError as exc:
        raise HttpError(422, "この level は既に使われています") from exc
    return _tier_out(tier)


@router.put("/admin/fanclub/creators/{creator_id}/tiers/{tier_id}", response=CreatorTierOut)
def tier_update(request: HttpRequest, creator_id: int, tier_id: int, payload: CreatorTierIn):
    tier = get_object_or_404(CreatorTier, pk=tier_id, creator_id=creator_id)
    if tier.level == 0:
        # level0(無料)は level/price 固定。name/description/is_active のみ変更可。
        if payload.level != 0 or payload.price_minor is not None:
            raise HttpError(422, "level0(無料)の level/price_minor は変更できません")
    elif payload.price_minor is None:
        raise HttpError(422, "有料ティアには price_minor が必要です")
    tier.level = payload.level
    tier.name = payload.name
    tier.description = payload.description
    tier.price_minor = payload.price_minor
    tier.is_active = payload.is_active
    tier.stripe_price_id = payload.stripe_price_id
    try:
        tier.save()
    except IntegrityError as exc:
        raise HttpError(422, "この level は既に使われています") from exc
    return _tier_out(tier)


@router.delete("/admin/fanclub/creators/{creator_id}/tiers/{tier_id}", response=OkOut)
def tier_delete(request: HttpRequest, creator_id: int, tier_id: int):
    tier = get_object_or_404(CreatorTier, pk=tier_id, creator_id=creator_id)
    if tier.level == 0:
        raise HttpError(422, "level0(無料)は削除できません")
    tier.delete()
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  招待                                                                       #
# --------------------------------------------------------------------------- #


def _invitation_out(inv: CreatorInvitation) -> dict:
    from django.conf import settings

    base = settings.ICSTV_CREATOR_BASE_URL.rstrip("/")
    return {
        "id": inv.id,
        "email": inv.email,
        "expires_at": inv.expires_at.isoformat(),
        "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else "",
        "created_at": inv.created_at.isoformat(),
        # ICSTV_CREATOR_BASE_URL 未設定 (dev既定) は相対パスのまま返す (studio側でコピーはできる)。
        "invite_url": f"{base}/invite/{inv.token}/",
    }


@router.get("/admin/fanclub/creators/{creator_id}/invitations", response=list[CreatorInvitationOut])
def invitations_list(request: HttpRequest, creator_id: int):
    creator = get_object_or_404(Creator, pk=creator_id)
    invs = creator.invitations.order_by("-created_at")
    return [_invitation_out(i) for i in invs]


@router.post("/admin/fanclub/creators/{creator_id}/invitations", response=CreatorInvitationOut)
def invitation_create(request: HttpRequest, creator_id: int, payload: CreatorInvitationIn):
    creator = get_object_or_404(Creator, pk=creator_id)
    email = payload.email.strip().lower()
    if not email:
        raise HttpError(422, "メールアドレスは必須です")
    inv = CreatorInvitation.objects.create(
        creator=creator,
        email=email,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(days=_INVITATION_TTL_DAYS),
    )
    _send_invitation_email(inv)
    return _invitation_out(inv)


def _send_invitation_email(inv: CreatorInvitation) -> None:
    """招待メール送信 (members/codes.py と同じ方針: 失敗しても招待レコード自体は成立させる)。"""
    import contextlib

    from django.conf import settings
    from django.core.mail import send_mail

    # 通知失敗は招待作成の成功に影響させない (studio 側で URL を直接コピーできる)。
    with contextlib.suppress(Exception):
        send_mail(
            subject=f"[ICS-TV] {inv.creator.name} のクリエイター招待",
            message=(
                f"{inv.creator.name} のクリエイターとして招待されました。\n\n"
                f"下記リンクから参加してください:\n{_invitation_out(inv)['invite_url']}\n\n"
                f"有効期限: {inv.expires_at:%Y-%m-%d %H:%M}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[inv.email],
            fail_silently=True,
        )


@router.delete("/admin/fanclub/creators/{creator_id}/invitations/{invitation_id}", response=OkOut)
def invitation_delete(request: HttpRequest, creator_id: int, invitation_id: int):
    inv = get_object_or_404(CreatorInvitation, pk=invitation_id, creator_id=creator_id)
    inv.delete()
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  枠契約 (SlotContract)                                                      #
# --------------------------------------------------------------------------- #


def _contract_out(sc: SlotContract) -> dict:
    return {
        "id": sc.id,
        "title": sc.title,
        "monthly_fee_minor": sc.monthly_fee_minor,
        "currency": sc.currency,
        "starts_on": sc.starts_on.isoformat(),
        "ends_on": sc.ends_on.isoformat() if sc.ends_on else "",
        "status": sc.status,
        "youtube_destination": sc.youtube_destination,
        "notes": sc.notes,
        "stripe_active": bool(sc.stripe_subscription_id),
    }


def _parse_date(s: str):
    from datetime import date

    return date.fromisoformat(s) if s else None


@router.get("/admin/fanclub/creators/{creator_id}/contracts", response=list[SlotContractOut])
def contracts_list(request: HttpRequest, creator_id: int):
    creator = get_object_or_404(Creator, pk=creator_id)
    return [_contract_out(c) for c in creator.slot_contracts.order_by("-starts_on")]


@router.post("/admin/fanclub/creators/{creator_id}/contracts", response=SlotContractOut)
def contract_create(request: HttpRequest, creator_id: int, payload: SlotContractIn):
    creator = get_object_or_404(Creator, pk=creator_id)
    starts_on = _parse_date(payload.starts_on)
    if starts_on is None:
        raise HttpError(422, "starts_on は必須です")
    sc = SlotContract.objects.create(
        creator=creator,
        title=payload.title,
        monthly_fee_minor=payload.monthly_fee_minor,
        starts_on=starts_on,
        ends_on=_parse_date(payload.ends_on),
        status=payload.status,
        youtube_destination=payload.youtube_destination,
        notes=payload.notes,
    )
    return _contract_out(sc)


@router.put(
    "/admin/fanclub/creators/{creator_id}/contracts/{contract_id}", response=SlotContractOut
)
def contract_update(
    request: HttpRequest, creator_id: int, contract_id: int, payload: SlotContractIn
):
    sc = get_object_or_404(SlotContract, pk=contract_id, creator_id=creator_id)
    starts_on = _parse_date(payload.starts_on)
    if starts_on is None:
        raise HttpError(422, "starts_on は必須です")
    # Stripe Billing 連携済みの契約は Webhook のみが status を更新する規律 (CreatorMembership と
    # 同じ)。手動で変えようとした場合のみ拒否する (無変更の PUT は許可)。
    if sc.stripe_subscription_id and payload.status != sc.status:
        raise HttpError(
            409, "Stripe Billing 連携済みの契約は状態を手動変更できません(Webhookが自動更新します)"
        )
    sc.title = payload.title
    sc.monthly_fee_minor = payload.monthly_fee_minor
    sc.starts_on = starts_on
    sc.ends_on = _parse_date(payload.ends_on)
    sc.status = payload.status
    sc.youtube_destination = payload.youtube_destination
    sc.notes = payload.notes
    sc.save()
    return _contract_out(sc)


@router.delete("/admin/fanclub/creators/{creator_id}/contracts/{contract_id}", response=OkOut)
def contract_delete(request: HttpRequest, creator_id: int, contract_id: int):
    sc = get_object_or_404(SlotContract, pk=contract_id, creator_id=creator_id)
    sc.delete()
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  会員サマリ                                                                 #
# --------------------------------------------------------------------------- #


@router.get(
    "/admin/fanclub/creators/{creator_id}/members-summary", response=CreatorMembersSummaryOut
)
def members_summary(request: HttpRequest, creator_id: int):
    creator = get_object_or_404(Creator, pk=creator_id)
    qs = CreatorMembership.objects.filter(creator=creator, status=MembershipStatus.ACTIVE)
    by_level: dict[str, int] = {}
    for row in qs.values("tier__level").order_by():
        key = str(row["tier__level"])
        by_level[key] = by_level.get(key, 0) + 1
    total = sum(by_level.values())
    return {"total": total, "by_level": by_level}


def _add_months(dt, n: int):
    y, m = dt.year, dt.month + n
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return dt.replace(year=y, month=m)


@router.get(
    "/admin/fanclub/creators/{creator_id}/revenue-summary", response=CreatorRevenueSummaryOut
)
def revenue_summary(request: HttpRequest, creator_id: int):
    """B2B枠契約MRR + FC会員の月次入会/退会・直近完了月のchurn率 (#27 Phase B)。

    churn_rate は月初在籍数を母数にした最新の**完了済み**月のみ算出する (当月は途中経過のため対象外)。
    """
    creator = get_object_or_404(Creator, pk=creator_id)

    # MRR は**通貨ごとに**出す。通貨をまたいで足すと「円とドルを足した数字」になり、
    # 一見それらしい値が出るぶん誤りに気付けない。slot_mrr_jpy は JPY 分のみ。
    active = creator.slot_contracts.filter(status=SlotContractStatus.ACTIVE)
    mrr_by_currency = list(
        active.values("currency").annotate(total=Sum("monthly_fee_minor")).order_by("currency")
    )
    jpy_mrr = next((r for r in mrr_by_currency if r["currency"] == "jpy"), None)
    fc_active_members = CreatorMembership.objects.filter(
        creator=creator, status=MembershipStatus.ACTIVE
    ).count()

    now = timezone.now()
    this_month_start = timezone.localtime(now).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    month_starts = [_add_months(this_month_start, -i) for i in range(5, -1, -1)]
    boundaries = [*month_starts, now]
    memberships = list(creator.memberships.values_list("joined_at", "left_at"))

    monthly = []
    churn_rate = None
    for i, start in enumerate(month_starts):
        end = boundaries[i + 1]
        joined = sum(1 for j, _left_at in memberships if j and start <= j < end)
        left = sum(1 for _j, left_at in memberships if left_at and start <= left_at < end)
        monthly.append({"month": start.strftime("%Y-%m"), "joined": joined, "left": left})
        if end != now:  # 完了済みの月のみ churn を採用 (最後まで回すと最新の完了月に収束する)
            active_at_start = sum(
                1
                for j, left_at in memberships
                if j and j < start and (not left_at or left_at >= start)
            )
            if active_at_start > 0:
                churn_rate = round(left / active_at_start, 4)

    return {
        "slot_mrr_jpy": (jpy_mrr["total"] if jpy_mrr else 0) or 0,
        "slot_mrr_by_currency": mrr_by_currency,
        "active_slot_contracts": active.count(),
        "fc_active_members": fc_active_members,
        "monthly": monthly,
        "churn_rate": churn_rate,
    }


# --------------------------------------------------------------------------- #
#  分配元帳 (#27 Phase B)                                                     #
# --------------------------------------------------------------------------- #


def _settlement_out(s: FcSettlement) -> dict:
    return {
        "id": s.id,
        "member_email": s.member.email if s.member else "",
        "tier_name": s.tier.name if s.tier else "",
        "gross_amount_minor": s.gross_amount_minor,
        "application_fee_minor": s.application_fee_minor,
        "net_amount_minor": s.net_amount_minor,
        "currency": s.currency,
        "disputed": s.disputed,
        "period_start": s.period_start.isoformat() if s.period_start else "",
        "period_end": s.period_end.isoformat() if s.period_end else "",
        "created_at": s.created_at.isoformat(),
    }


@router.get("/admin/fanclub/creators/{creator_id}/settlements", response=FcSettlementSummaryOut)
def settlements_list(request: HttpRequest, creator_id: int):
    """直近の分配元帳(新しい順、最大 _SETTLEMENT_PAGE_SIZE 件)+ 累計サマリ。"""
    creator = get_object_or_404(Creator, pk=creator_id)
    qs = creator.settlements.select_related("member", "tier").order_by("-created_at")
    items = list(qs[:_SETTLEMENT_PAGE_SIZE])
    # **合計は通貨ごとに出す。** 通貨をまたいで足すと「円とドルを足した数字」になり、
    # 一見それらしい値が出るぶん誤りに気付けない。
    by_currency = list(
        qs.values("currency")
        .annotate(
            total_gross=Sum("gross_amount_minor"),
            total_fee=Sum("application_fee_minor"),
            total_net=Sum("net_amount_minor"),
        )
        .order_by("currency")
    )
    # total_*_jpy は studio SPA (frontend/) が参照しているため残す。JPY 分だけを入れ、
    # 外貨は totals_by_currency 側で見る。SPA が対応したら削れる。
    jpy = next((r for r in by_currency if r["currency"] == "jpy"), None)
    return {
        "total_gross_jpy": (jpy["total_gross"] if jpy else 0) or 0,
        "total_fee_jpy": (jpy["total_fee"] if jpy else 0) or 0,
        "total_net_jpy": (jpy["total_net"] if jpy else 0) or 0,
        "totals_by_currency": by_currency,
        "disputed_count": qs.filter(disputed=True).count(),
        "items": [_settlement_out(s) for s in items],
    }
