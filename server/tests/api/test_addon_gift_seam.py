# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ギフト/投げ銭 追加側サービス seam (M3a/M3b リハーサル): /internal/addon/*。

独立DB・独立 Stripe webhook を持つ追加側が、期限付きの在籍格上げを本体へ登録する。
X-Internal-Token (ADDON_GIFT_TOKEN) 認証。登録の冪等性・書込所有者の分離・再配送を検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub.models import (
    Creator,
    CreatorAddonGrant,
    CreatorMembership,
    CreatorTier,
    MembershipStatus,
)
from members.models import Member

TOK = "addon-gift-tok"  # pragma: allowlist secret - test only
_OVR = override_settings(ADDON_GIFT_TOKEN=TOK)


def _post(path, body, token=TOK):
    return Client().post(
        path,
        data=body,
        content_type="application/json",
        headers={"X-Internal-Token": token} if token else {},
    )


def _get(path, token=TOK):
    return Client().get(path, headers={"X-Internal-Token": token} if token else {})


def _creator_and_tier(level=1):
    c = Creator.objects.create(name="circle", slug="circle")
    fc_services.ensure_free_tier(c)
    t = CreatorTier.objects.create(creator=c, level=level, name="basic", price_minor=500)
    return c, t


def _member(email="m@example.invalid"):
    m = Member(email=email, nickname="m", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password("rehearsal-only-Pw1!")  # pragma: allowlist secret - test only
    m.save()
    return m


@_OVR
def test_capabilities_requires_token_and_reports_version(db):
    assert _get("/api/v1/internal/addon/capabilities", token="").status_code == 401
    assert _get("/api/v1/internal/addon/capabilities", token="wrong").status_code == 401
    r = _get("/api/v1/internal/addon/capabilities")
    assert r.status_code == 200
    assert r.json() == {"gift_redeem_api_version": 1}


def test_capabilities_token_unset_is_401(db):
    assert _get("/api/v1/internal/addon/capabilities").status_code == 401


@_OVR
def test_redeem_requires_token(db):
    c, t = _creator_and_tier()
    m = _member()
    body = {
        "external_reference": "code1",
        "creator_slug": c.slug,
        "tier_level": t.level,
        "member_email": m.email,
        "months": 1,
    }
    assert _post("/api/v1/internal/addon/gift-redeem", body, token="").status_code == 401
    assert _post("/api/v1/internal/addon/gift-redeem", body, token="wrong").status_code == 401


@_OVR
def test_redeem_grants_tier_and_is_idempotent_on_redelivery(db):
    c, t = _creator_and_tier()
    m = _member()
    body = {
        "external_reference": "code1",
        "creator_slug": c.slug,
        "tier_level": t.level,
        "member_email": m.email,
        "months": 2,
    }
    assert fc_services.member_level(m, c) is None

    r1 = _post("/api/v1/internal/addon/gift-redeem", body)
    assert r1.status_code == 200
    assert r1.json()["granted"] is True and r1.json()["tier_level"] == t.level
    assert fc_services.member_level(m, c) == t.level
    assert CreatorAddonGrant.objects.count() == 1

    # redelivery of the same event: same result, no second row, no expiry extension
    r2 = _post("/api/v1/internal/addon/gift-redeem", body)
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert CreatorAddonGrant.objects.count() == 1


@_OVR
def test_redeem_unknown_creator_tier_or_member_is_404(db):
    c, t = _creator_and_tier()
    m = _member()
    base = {
        "external_reference": "code1",
        "creator_slug": c.slug,
        "tier_level": t.level,
        "member_email": m.email,
        "months": 1,
    }
    assert (
        _post("/api/v1/internal/addon/gift-redeem", {**base, "creator_slug": "nope"}).status_code
        == 404
    )
    assert (
        _post("/api/v1/internal/addon/gift-redeem", {**base, "tier_level": 99}).status_code == 404
    )
    assert (
        _post(
            "/api/v1/internal/addon/gift-redeem", {**base, "member_email": "nope@example.invalid"}
        ).status_code
        == 404
    )
    assert CreatorAddonGrant.objects.count() == 0


@_OVR
def test_grant_does_not_touch_an_existing_stripe_membership(db):
    """書込所有者の分離: 追加側の格付けは、既存の Stripe 購読による在籍を書き換えない。"""
    c, t = _creator_and_tier(level=2)
    lower = CreatorTier.objects.create(creator=c, level=1, name="lower", price_minor=300)
    m = _member()
    membership = CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=lower,
        status=MembershipStatus.ACTIVE,
        stripe_subscription_id="sub_real",
    )
    body = {
        "external_reference": "code1",
        "creator_slug": c.slug,
        "tier_level": t.level,
        "member_email": m.email,
        "months": 1,
    }
    _post("/api/v1/internal/addon/gift-redeem", body)
    membership.refresh_from_db()
    assert membership.tier_id == lower.id and membership.stripe_subscription_id == "sub_real"
    # the effective level is the higher of the two, without altering the Stripe-owned row
    assert fc_services.member_level(m, c) == t.level


@_OVR
def test_expired_grant_does_not_grant_access(db):
    c, t = _creator_and_tier()
    m = _member()
    CreatorAddonGrant.objects.create(
        creator=c,
        member=m,
        tier=t,
        addon_name="gift",
        external_reference="old",
        expires_at=timezone.now() - timedelta(days=1),
    )
    assert fc_services.member_level(m, c) is None
