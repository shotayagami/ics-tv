# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理: 退会 (アカウント削除。パスワード再入力 + PII ハード削除)。"""

from __future__ import annotations

import pytest
from django.test import override_settings

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorMembership, CreatorTier, MembershipStatus
from members.auth import SESSION_KEY
from members.models import CodePurpose, Member, MemberEmailCode
from subscriptions.models import MemberSubscription, SubStatus

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_WRONG = "nope-wrong-1!"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client):
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})


@_PUBLIC_HOST
def test_delete_requires_login(http_client, db):
    res = http_client.get("/members/delete/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_delete_removes_member_and_codes_and_session(http_client, db):
    m = _make_member()
    MemberEmailCode.objects.create(
        member=m, purpose=CodePurpose.EMAIL_VERIFY, code_hash="x", expires_at=m.created_at
    )
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW, "confirm": "on"})
    assert res.status_code == 302 and res.url == "/"
    assert not Member.objects.filter(pk=m.pk).exists()
    assert not MemberEmailCode.objects.filter(member_id=m.pk).exists()  # CASCADE
    assert SESSION_KEY not in http_client.session


@_PUBLIC_HOST
def test_delete_wrong_password_keeps_account(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _WRONG, "confirm": "on"})
    assert res.status_code == 200
    assert Member.objects.filter(pk=m.pk).exists()


@_PUBLIC_HOST
def test_delete_requires_confirm_checkbox(http_client, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW})  # confirm 無し
    assert res.status_code == 200
    assert Member.objects.filter(pk=m.pk).exists()


# ---- 有効なサブスクがある間は退会させない (Stripe の購読は退会では解約されない) ----


def _paid_membership(member, *, status=MembershipStatus.ACTIVE, **kw):
    creator = Creator.objects.create(name="サークルA", slug="circle-a")
    fc_services.ensure_free_tier(creator)
    tier = CreatorTier.objects.create(
        creator=creator, level=1, name="ベーシック", price_minor=500, stripe_price_id="price_fc"
    )
    return CreatorMembership.objects.create(
        member=member,
        creator=creator,
        tier=tier,
        status=status,
        stripe_subscription_id="sub_fc",
        **kw,
    )


def _refused(http_client, member):
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW, "confirm": "on"})
    assert res.status_code == 200
    assert "サブスクリプション" in res.content.decode("utf-8")
    assert Member.objects.filter(pk=member.pk).exists()


@_PUBLIC_HOST
@pytest.mark.parametrize(
    "status", [SubStatus.ACTIVE, SubStatus.TRIALING, SubStatus.PAST_DUE, SubStatus.UNPAID]
)
def test_delete_refused_while_viewer_subscription_is_billing(http_client, db, status):
    m = _make_member()
    MemberSubscription.objects.create(member=m, status=status, stripe_subscription_id="sub_v")
    _refused(http_client, m)


@_PUBLIC_HOST
def test_delete_refused_while_fanclub_paid_membership_is_active(http_client, db):
    m = _make_member()
    _paid_membership(m)
    _refused(http_client, m)


@_PUBLIC_HOST
def test_delete_allowed_once_subscriptions_are_ended(http_client, db):
    m = _make_member()
    MemberSubscription.objects.create(
        member=m, status=SubStatus.CANCELED, stripe_subscription_id="sub_v"
    )
    _paid_membership(m, status=MembershipStatus.LEFT)
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW, "confirm": "on"})
    assert res.status_code == 302 and res.url == "/"
    assert not Member.objects.filter(pk=m.pk).exists()


@_PUBLIC_HOST
def test_delete_allowed_when_cancellation_at_period_end_is_already_scheduled(http_client, db):
    m = _make_member()
    MemberSubscription.objects.create(
        member=m, status=SubStatus.ACTIVE, stripe_subscription_id="sub_v", cancel_at_period_end=True
    )
    _paid_membership(m, cancel_at_period_end=True)
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW, "confirm": "on"})
    assert res.status_code == 302 and res.url == "/"
    assert not Member.objects.filter(pk=m.pk).exists()


@_PUBLIC_HOST
def test_delete_allowed_for_free_membership(http_client, db):
    m = _make_member()
    creator = Creator.objects.create(name="サークルA", slug="circle-a")
    fc_services.ensure_free_tier(creator)
    fc_services.join_free_tier(m, creator)
    _login(http_client)
    res = http_client.post("/members/delete/", {"password": _PW, "confirm": "on"})
    assert res.status_code == 302 and res.url == "/"
