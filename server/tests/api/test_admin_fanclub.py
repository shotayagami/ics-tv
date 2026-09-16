# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio admin API: Creator/ティア/招待/枠契約/会員サマリ (#27)。"""

from __future__ import annotations

import json

from django.core import mail

from fanclub.models import (
    Creator,
    CreatorInvitation,
    CreatorMembership,
    CreatorSeriesLink,
    CreatorTier,
    SlotContract,
)
from scheduling.models import Series, SeriesPost

BASE = "/api/v1/admin/fanclub/creators"


def _creator(slug="circle-a"):
    from fanclub.services import ensure_free_tier

    c = Creator.objects.create(name="サークルA", slug=slug)
    ensure_free_tier(c)  # admin/API 経由の実運用と同じく level0 を常に持たせる
    return c


def _series(channel, title="番組A"):
    return Series.objects.create(channel=channel, title=title)


# ---- 認証 ----


def test_creators_list_requires_staff(http_client, db):
    assert http_client.get(BASE).status_code == 401


def test_creators_list_ok_for_staff(staff_client, db):
    assert staff_client.get(BASE).status_code == 200


# ---- Creator CRUD ----


def test_creator_create_auto_creates_free_tier(staff_client, db):
    body = {"name": "新規サークル", "slug": "new-circle", "description": ""}
    resp = staff_client.post(BASE, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200
    creator = Creator.objects.get(slug="new-circle")
    assert CreatorTier.objects.filter(creator=creator, level=0).exists()


def test_creator_create_duplicate_slug_422(staff_client, db):
    _creator(slug="dup")
    body = {"name": "別サークル", "slug": "dup"}
    resp = staff_client.post(BASE, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 422


def test_creator_update(staff_client, db):
    c = _creator()
    body = {"name": "改名後", "slug": c.slug, "status": "suspended"}
    resp = staff_client.put(
        f"{BASE}/{c.id}", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    c.refresh_from_db()
    assert c.name == "改名後"
    assert c.status == "suspended"


# ---- オンボーディング (#27 Phase B、本人確認・特商法表記) ----


def test_creator_create_defaults_onboarding_pending_and_hides_contact(staff_client, db):
    body = {"name": "新規サークル2", "slug": "new-circle-2"}
    resp = staff_client.post(BASE, data=json.dumps(body), content_type="application/json")
    data = resp.json()
    assert data["onboarding_status"] == "pending"
    assert data["hide_contact_details"] is True
    assert data["identity_verified_at"] == ""


def test_creator_update_tokushoho_fields(staff_client, db):
    c = _creator()
    body = {
        "name": c.name,
        "slug": c.slug,
        "onboarding_status": "approved",
        "legal_name": "山田太郎",
        "is_individual": True,
        "representative_name": "",
        "address": "東京都千代田区1-1-1",
        "phone": "03-0000-0000",
        "hide_contact_details": False,
        "contact_email": "creator@example.com",
        "invoice_registration_number": "T1234567890123",
    }
    resp = staff_client.put(
        f"{BASE}/{c.id}", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["onboarding_status"] == "approved"
    assert data["legal_name"] == "山田太郎"
    assert data["hide_contact_details"] is False
    assert data["invoice_registration_number"] == "T1234567890123"


def test_creator_verify_identity(staff_client, db):
    c = _creator()
    assert c.identity_verified_at is None
    resp = staff_client.post(f"{BASE}/{c.id}/verify-identity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["identity_verified_at"] != ""
    c.refresh_from_db()
    assert c.identity_verified_at is not None


# ---- Series 紐付け ----


def test_series_options_shows_linked_creator_name(staff_client, channel, db):
    c1 = _creator(slug="c1")
    s = _series(channel)
    CreatorSeriesLink.objects.create(series=s, creator=c1)
    data = staff_client.get("/api/v1/admin/fanclub/series-options?q=番組").json()
    row = next(r for r in data if r["id"] == s.id)
    assert row["linked_creator_name"] == "サークルA"


def test_series_link_and_duplicate_rejected(staff_client, channel, db):
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    s = _series(channel)
    resp = staff_client.post(f"{BASE}/{c1.id}/series?series_id={s.id}")
    assert resp.status_code == 200
    resp2 = staff_client.post(f"{BASE}/{c2.id}/series?series_id={s.id}")
    assert resp2.status_code == 422


def test_series_unlink(staff_client, channel, db):
    c = _creator()
    s = _series(channel)
    CreatorSeriesLink.objects.create(series=s, creator=c)
    resp = staff_client.delete(f"{BASE}/{c.id}/series/{s.id}")
    assert resp.status_code == 200
    assert not CreatorSeriesLink.objects.filter(series=s).exists()


# ---- ティア ----


def test_tier_create_paid(staff_client, db):
    c = _creator()
    body = {"level": 1, "name": "ベーシック", "price_minor": 500}
    resp = staff_client.post(
        f"{BASE}/{c.id}/tiers", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert resp.json()["joinable"] is False  # Phase A は level0 のみ加入可能


def test_tier_create_level0_rejected(staff_client, db):
    c = _creator()
    body = {"level": 0, "name": "無料2", "price_minor": None}
    resp = staff_client.post(
        f"{BASE}/{c.id}/tiers", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 422


def test_tier_delete_level0_rejected(staff_client, db):
    c = _creator()
    tier = CreatorTier.objects.get(creator=c, level=0)
    resp = staff_client.delete(f"{BASE}/{c.id}/tiers/{tier.id}")
    assert resp.status_code == 422


def test_tier_update_and_delete_paid(staff_client, db):
    c = _creator()
    tier = CreatorTier.objects.create(creator=c, level=1, name="B", price_minor=500)
    body = {"level": 1, "name": "B改", "price_minor": 800}
    resp = staff_client.put(
        f"{BASE}/{c.id}/tiers/{tier.id}", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200 and resp.json()["price_minor"] == 800
    resp2 = staff_client.delete(f"{BASE}/{c.id}/tiers/{tier.id}")
    assert resp2.status_code == 200


# ---- 招待 ----


def test_invitation_create_sends_mail_and_returns_url(staff_client, db, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    c = _creator()
    body = {"email": "creator@example.com"}
    resp = staff_client.post(
        f"{BASE}/{c.id}/invitations", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["invite_url"].startswith("/invite/")
    inv = CreatorInvitation.objects.get(creator=c, email="creator@example.com")
    assert inv.token in data["invite_url"]
    assert len(mail.outbox) == 1
    assert "creator@example.com" in mail.outbox[0].to


def test_invitation_create_absolute_url_when_creator_base_url_configured(
    staff_client, db, settings
):
    """ICSTV_CREATOR_BASE_URL 設定時は招待URLが絶対URLになる (#27 PR7 との統合)。"""
    settings.ICSTV_CREATOR_BASE_URL = "https://creator.example.com"
    c = _creator()
    resp = staff_client.post(
        f"{BASE}/{c.id}/invitations",
        data=json.dumps({"email": "creator2@example.com"}),
        content_type="application/json",
    )
    data = resp.json()
    assert data["invite_url"].startswith("https://creator.example.com/invite/")


def test_invitation_delete(staff_client, db):
    c = _creator()
    inv = CreatorInvitation.objects.create(
        creator=c, email="x@example.com", token="tok123", expires_at="2030-01-01T00:00:00Z"
    )
    resp = staff_client.delete(f"{BASE}/{c.id}/invitations/{inv.id}")
    assert resp.status_code == 200
    assert not CreatorInvitation.objects.filter(pk=inv.id).exists()


# ---- 枠契約 ----


def test_contract_crud(staff_client, db):
    c = _creator()
    body = {
        "title": "レギュラー枠",
        "monthly_fee_minor": 100000,
        "starts_on": "2026-08-01",
        "status": "draft",
        "youtube_destination": "none",
    }
    resp = staff_client.post(
        f"{BASE}/{c.id}/contracts", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    contract_id = resp.json()["id"]

    body2 = {**body, "status": "active"}
    resp2 = staff_client.put(
        f"{BASE}/{c.id}/contracts/{contract_id}",
        data=json.dumps(body2),
        content_type="application/json",
    )
    assert resp2.status_code == 200 and resp2.json()["status"] == "active"

    resp3 = staff_client.delete(f"{BASE}/{c.id}/contracts/{contract_id}")
    assert resp3.status_code == 200
    assert not SlotContract.objects.filter(pk=contract_id).exists()


def test_contract_stripe_active_flag(staff_client, db):
    c = _creator()
    sc = SlotContract.objects.create(
        creator=c,
        title="Stripe連携枠",
        monthly_fee_minor=50000,
        starts_on="2026-08-01",
        status="active",
        stripe_subscription_id="sub_x",
    )
    resp = staff_client.get(f"{BASE}/{c.id}/contracts")
    row = next(r for r in resp.json() if r["id"] == sc.id)
    assert row["stripe_active"] is True
    # 金額は minor unit なので、通貨を伴わないと表示側で ¥ 決め打ちになる
    assert row["currency"] == "jpy"


def test_contract_stripe_active_rejects_manual_status_change(staff_client, db):
    c = _creator()
    sc = SlotContract.objects.create(
        creator=c,
        title="Stripe連携枠",
        monthly_fee_minor=50000,
        starts_on="2026-08-01",
        status="active",
        stripe_subscription_id="sub_x",
    )
    body = {
        "title": sc.title,
        "monthly_fee_minor": sc.monthly_fee_minor,
        "starts_on": "2026-08-01",
        "status": "suspended",
        "youtube_destination": "none",
    }
    resp = staff_client.put(
        f"{BASE}/{c.id}/contracts/{sc.id}", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 409
    sc.refresh_from_db()
    assert sc.status == "active"  # 変更されていない


def test_contract_stripe_active_allows_other_field_updates(staff_client, db):
    """status を変えない更新(タイトル等)はStripe連携済みでも許可される。"""
    c = _creator()
    sc = SlotContract.objects.create(
        creator=c,
        title="旧タイトル",
        monthly_fee_minor=50000,
        starts_on="2026-08-01",
        status="active",
        stripe_subscription_id="sub_x",
    )
    body = {
        "title": "新タイトル",
        "monthly_fee_minor": sc.monthly_fee_minor,
        "starts_on": "2026-08-01",
        "status": "active",
        "youtube_destination": "none",
    }
    resp = staff_client.put(
        f"{BASE}/{c.id}/contracts/{sc.id}", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    sc.refresh_from_db()
    assert sc.title == "新タイトル"


# ---- 会員サマリ ----


def test_members_summary(staff_client, db):
    from fanclub import services as fc_services
    from members.models import Member

    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    m = Member(
        email="m@example.com", nickname="t", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
    m.save()
    fc_services.join(m, c, tier)
    data = staff_client.get(f"{BASE}/{c.id}/members-summary").json()
    assert data["total"] == 1
    assert data["by_level"]["0"] == 1


# ---- 収益サマリ (#27 Phase B) ----


def _months_ago(n: int):
    from django.utils import timezone

    this_month_start = timezone.localtime(timezone.now()).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    y, m = this_month_start.year, this_month_start.month - n
    while m < 1:
        m += 12
        y -= 1
    return this_month_start.replace(year=y, month=m)


def test_revenue_summary(staff_client, db):
    from datetime import timedelta

    from fanclub import services as fc_services
    from members.models import Member

    c = _creator()
    tier = fc_services.ensure_free_tier(c)

    SlotContract.objects.create(
        creator=c,
        title="レギュラー枠",
        monthly_fee_minor=50000,
        starts_on="2026-01-01",
        status="active",
        youtube_destination="none",
    )
    SlotContract.objects.create(
        creator=c,
        title="下書き枠(集計対象外)",
        monthly_fee_minor=99999,
        starts_on="2026-01-01",
        status="draft",
        youtube_destination="none",
    )

    def _member(email):
        m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
        m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
        m.save()
        return m

    m1 = _member("m1@example.com")
    fc_services.join(m1, c, tier)
    CreatorMembership.objects.filter(member=m1, creator=c).update(joined_at=_months_ago(2))

    m2 = _member("m2@example.com")
    fc_services.join(m2, c, tier)
    CreatorMembership.objects.filter(member=m2, creator=c).update(
        joined_at=_months_ago(3),
        status="left",
        left_at=_months_ago(1) + timedelta(days=2),
    )

    m3 = _member("m3@example.com")
    fc_services.join(m3, c, tier)  # 当月加入 (在籍中)

    data = staff_client.get(f"{BASE}/{c.id}/revenue-summary").json()
    assert data["slot_mrr_jpy"] == 50000
    assert data["slot_mrr_by_currency"] == [{"currency": "jpy", "total": 50000}]
    assert data["active_slot_contracts"] == 1
    assert data["fc_active_members"] == 2  # m1, m3 (m2 は退会済)
    assert len(data["monthly"]) == 6

    by_month = {row["month"]: row for row in data["monthly"]}
    assert by_month[_months_ago(2).strftime("%Y-%m")]["joined"] == 1
    assert by_month[_months_ago(1).strftime("%Y-%m")]["left"] == 1
    # 直近の完了月(1ヶ月前)時点で在籍していたのは m1・m2 の2名、うち退会は m2 の1名
    assert data["churn_rate"] == 0.5


def test_revenue_summary_does_not_mix_currencies(staff_client, db):
    """通貨をまたいだ合算はしない。slot_mrr_jpy は JPY 分のみで、外貨は内訳側に出す。

    足してしまうと「円とドルを足した数字」になり、一見それらしい値が出るぶん誤りに気付けない。
    """
    c = _creator()
    SlotContract.objects.create(
        creator=c,
        title="国内枠",
        monthly_fee_minor=50000,
        starts_on="2026-01-01",
        status="active",
        youtube_destination="none",
    )
    SlotContract.objects.create(
        creator=c,
        title="海外枠",
        monthly_fee_minor=30000,
        currency="usd",
        starts_on="2026-01-01",
        status="active",
        youtube_destination="none",
    )

    data = staff_client.get(f"{BASE}/{c.id}/revenue-summary").json()
    assert data["slot_mrr_jpy"] == 50000  # 80000 にはしない
    assert data["slot_mrr_by_currency"] == [
        {"currency": "jpy", "total": 50000},
        {"currency": "usd", "total": 30000},
    ]
    assert data["active_slot_contracts"] == 2  # 件数は通貨に関係なく数える


# ---- SeriesPost CRUD ----

POSTS_BASE = "/api/v1/admin/scheduling/{slug}/series/{series_id}/posts"


def test_series_post_crud_with_fc_required_level(staff_client, channel, db):
    s = _series(channel)
    url = POSTS_BASE.format(slug=channel.slug, series_id=s.id)
    body = {"kind": "article", "title": "限定記事", "body": "本文", "fc_required_level": 0}
    resp = staff_client.post(url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["fc_required_level"] == 0
    post_id = data["id"]

    listed = staff_client.get(url).json()
    assert any(p["id"] == post_id for p in listed)

    body2 = {**body, "title": "改題", "is_published": True}
    resp2 = staff_client.put(
        f"{url}/{post_id}", data=json.dumps(body2), content_type="application/json"
    )
    assert resp2.status_code == 200
    assert resp2.json()["title"] == "改題"
    post = SeriesPost.objects.get(pk=post_id)
    assert post.is_published is True
    assert post.published_at is not None

    resp3 = staff_client.delete(f"{url}/{post_id}")
    assert resp3.status_code == 200
    assert not SeriesPost.objects.filter(pk=post_id).exists()


def test_series_post_default_level_null(staff_client, channel, db):
    s = _series(channel)
    url = POSTS_BASE.format(slug=channel.slug, series_id=s.id)
    body = {"kind": "article", "title": "通常記事"}
    resp = staff_client.post(url, data=json.dumps(body), content_type="application/json")
    assert resp.json()["fc_required_level"] is None
