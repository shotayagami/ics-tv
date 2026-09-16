# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開サイトの参加/退会 (fanclub/views.py join・leave) + series_detail のパネル表示。"""

from __future__ import annotations

from django.test import Client, override_settings

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorMembership, CreatorSeriesLink, CreatorStatus
from members.models import Member
from scheduling.models import Series

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a", status=CreatorStatus.ACTIVE):
    return Creator.objects.create(name="サークルA", slug=slug, status=status)


def _series(channel, creator=None, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    if creator is not None:
        CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


# ---- join ----


@_PUBLIC_HOST
def test_join_anonymous_redirects_to_login(http_client, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    res = http_client.post(f"/fanclub/{c.slug}/join/")
    assert res.status_code == 302
    assert res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_join_creates_free_membership(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/join/", {"next": s.public_url})
    assert res.status_code == 302 and res.url == s.public_url
    m = Member.objects.get(email="m@example.com")
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == "active"
    assert membership.tier.level == 0


@_PUBLIC_HOST
def test_join_idempotent_no_duplicate_membership(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    _member()
    _login(http_client)
    http_client.post(f"/fanclub/{c.slug}/join/")
    http_client.post(f"/fanclub/{c.slug}/join/")
    m = Member.objects.get(email="m@example.com")
    assert CreatorMembership.objects.filter(member=m, creator=c).count() == 1


@_PUBLIC_HOST
def test_join_suspended_creator_404(http_client, db):
    c = _creator(status=CreatorStatus.SUSPENDED)
    fc_services.ensure_free_tier(c)
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/join/")
    assert res.status_code == 404


@_PUBLIC_HOST
def test_join_without_free_tier_shows_error_message(http_client, db):
    c = _creator()
    # 意図的に無料ティアを作らない (通常は admin/services 経由で必ず作られるが異常系として検証)
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/join/", follow=True)
    msgs = [str(m) for m in res.context["messages"]]
    assert any("準備中" in m or "まだ" in m for m in msgs)


@_PUBLIC_HOST
def test_join_open_redirect_falls_back_to_default(http_client, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/join/", {"next": "https://evil.example/"})
    assert res.status_code == 302
    assert res.url == "/"  # 危険な next は破棄しデフォルトへ


@_PUBLIC_HOST
def test_join_requires_post(http_client, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    _member()
    _login(http_client)
    res = http_client.get(f"/fanclub/{c.slug}/join/")
    assert res.status_code == 405


def test_join_csrf_enforced(channel, db):
    """CSRF トークン無しの POST は拒否される (enforce_csrf_checks)。"""
    c = _creator()
    fc_services.ensure_free_tier(c)
    _member()
    strict_client = Client(enforce_csrf_checks=True)
    with override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[]):
        strict_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
        res = strict_client.post(f"/fanclub/{c.slug}/join/")
    assert res.status_code == 403


# ---- leave ----


@_PUBLIC_HOST
def test_leave_sets_left_status(http_client, db):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/leave/")
    assert res.status_code == 302
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == "left"


@_PUBLIC_HOST
def test_leave_noop_when_not_a_member(http_client, db):
    c = _creator()
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/leave/")
    assert res.status_code == 302
    assert not CreatorMembership.objects.filter(creator=c).exists()


# ---- series_detail パネル表示 ----


@_PUBLIC_HOST
def test_series_detail_shows_join_button_when_not_member(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    _member()
    _login(http_client)
    res = http_client.get(s.public_url)
    body = res.content.decode("utf-8")
    assert "ファンクラブに参加" in body


@_PUBLIC_HOST
def test_series_detail_shows_joined_state(http_client, channel, db):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)
    res = http_client.get(s.public_url)
    body = res.content.decode("utf-8")
    assert "会員です" in body


@_PUBLIC_HOST
def test_series_detail_no_fc_card_when_unlinked(http_client, channel, db):
    s = _series(channel, creator=None)
    res = http_client.get(s.public_url)
    body = res.content.decode("utf-8")
    assert "ファンクラブに参加" not in body


@_PUBLIC_HOST
def test_series_detail_links_to_fc_tokushoho(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    res = http_client.get(s.public_url)
    body = res.content.decode("utf-8")
    assert f"/fanclub/{c.slug}/tokushoho/" in body


# ---- クリエイター単位の特定商取引法に基づく表記 (#27 Phase B) ----


@_PUBLIC_HOST
def test_fc_tokushoho_page_renders(http_client, db):
    c = _creator()
    res = http_client.get(f"/fanclub/{c.slug}/tokushoho/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "特定商取引法に基づく表記" in body
    assert "支払方法" in body and "解約" in body and "返金" in body


@_PUBLIC_HOST
def test_fc_tokushoho_shows_disclose_on_request_when_hidden(http_client, db):
    c = _creator()
    c.hide_contact_details = True
    c.address = "非公開住所"
    c.save()
    body = http_client.get(f"/fanclub/{c.slug}/tokushoho/").content.decode("utf-8")
    assert "遅滞なく開示" in body
    assert "非公開住所" not in body


@_PUBLIC_HOST
def test_fc_tokushoho_shows_address_when_not_hidden(http_client, db):
    c = _creator()
    c.hide_contact_details = False
    c.address = "東京都千代田区1-1-1"
    c.save()
    body = http_client.get(f"/fanclub/{c.slug}/tokushoho/").content.decode("utf-8")
    assert "東京都千代田区1-1-1" in body


@_PUBLIC_HOST
def test_fc_tokushoho_lists_paid_tier_prices(http_client, db):
    from fanclub.models import CreatorTier

    c = _creator()
    fc_services.ensure_free_tier(c)
    CreatorTier.objects.create(creator=c, level=1, name="ベーシック", price_minor=500)
    body = http_client.get(f"/fanclub/{c.slug}/tokushoho/").content.decode("utf-8")
    assert "500円" in body


@_PUBLIC_HOST
def test_fc_tokushoho_404_for_unknown_creator(http_client, db):
    res = http_client.get("/fanclub/no-such-creator/tokushoho/")
    assert res.status_code == 404
