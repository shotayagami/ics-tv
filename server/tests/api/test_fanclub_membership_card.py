# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""デジタル会員証と加入中ファンクラブ一覧 (#27 §3.1 Must「デジタル会員証（会員番号表示）」)。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub import webhook as wh
from fanclub.models import Creator, CreatorMembership, CreatorTier, MembershipStatus
from members.models import Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", nickname="たろう"):
    m = Member(
        email=email, nickname=nickname, birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a", name="サークルA"):
    c = Creator.objects.create(
        name=name, slug=slug, stripe_connect_account_id="acct_test", stripe_connect_onboarded=True
    )
    fc_services.ensure_free_tier(c)
    return c


@contextmanager
def _frozen_now(fixed):
    """`enrolled_months` が見る「現在時刻」を固定する。

    `CreatorMembership.enrolled_months` は `timezone.localtime(self.joined_at)` (引数あり) と
    `timezone.localtime()` (引数なし = 現在時刻) の両方を使うため、引数の有無で振る舞いを
    分けて差し替える。freezegun / time-machine は未導入なので標準ライブラリだけで済ませる。
    """
    real = timezone.localtime

    def fake(value=None):
        return real(value) if value is not None else fixed

    with patch("fanclub.models.timezone.localtime", side_effect=fake):
        yield


def _months_ago(n: int):
    """n ヶ月前の同じ時刻 (実行日に依存しないよう日は28以下へ丸める)。

    注意: 丸められるのは `joined_at` 側だけで、`enrolled_months` が比較する「現在」は
    丸められない。したがって「応当日の前後」を突く用途 (+1 日など) にこの helper を
    使うと実行日に依存する。その手のケースは `_frozen_now` で時刻を固定すること。
    """
    now = timezone.localtime().replace(day=min(timezone.localtime().day, 28))
    month, year = now.month - n, now.year
    while month <= 0:
        month += 12
        year -= 1
    return now.replace(year=year, month=month)


def _paid_tier(creator, *, level=1, price_minor=500, price_id="price_l1"):
    return CreatorTier.objects.create(
        creator=creator,
        level=level,
        name=f"ティア{level}",
        price_minor=price_minor,
        stripe_price_id=price_id,
    )


# ---- 採番 ----


def test_join_assigns_member_no_from_one(db):
    c = _creator()
    m1 = fc_services.join_free_tier(_member("a@example.com"), c)
    m2 = fc_services.join_free_tier(_member("b@example.com"), c)
    assert (m1.member_no, m2.member_no) == (1, 2)


def test_member_no_is_per_creator(db):
    ca, cb = _creator("circle-a"), _creator("circle-b", name="サークルB")
    m = _member()
    assert fc_services.join_free_tier(m, ca).member_no == 1
    assert fc_services.join_free_tier(m, cb).member_no == 1


def test_member_no_survives_rejoin(db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(_member("first@example.com"), c)  # No.1 を先に埋める
    mine = fc_services.join_free_tier(m, c)
    assert mine.member_no == 2
    fc_services.leave(m, c)
    fc_services.join_free_tier(_member("later@example.com"), c)  # No.3
    again = fc_services.join_free_tier(m, c)
    assert again.member_no == 2  # 行を再利用するので番号も同じ


def test_ensure_member_no_is_idempotent(db):
    c = _creator()
    membership = fc_services.join_free_tier(_member(), c)
    first = membership.member_no
    assert fc_services.ensure_member_no(membership) == first
    membership.refresh_from_db()
    assert membership.member_no == first


def test_webhook_paid_join_assigns_member_no(db):
    c = _creator()
    t = _paid_tier(c)
    m = _member()
    wh.handle_event(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {
                        "member_id": str(m.pk),
                        "creator_id": str(c.pk),
                        "tier_id": str(t.pk),
                    },
                    "customer": "cus_1",
                    "subscription": "sub_1",
                }
            },
        }
    )
    assert CreatorMembership.objects.get(member=m, creator=c).member_no == 1


# ---- 表示用プロパティ ----


def test_member_no_display_is_zero_padded(db):
    c = _creator()
    membership = fc_services.join_free_tier(_member(), c)
    assert membership.member_no_display == "00001"


def test_member_no_display_when_unassigned(db):
    c = _creator()
    membership = fc_services.join_free_tier(_member(), c)
    membership.member_no = None
    assert membership.member_no_display == "—"


def test_enrolled_months_counts_only_completed_months(db):
    # 「応当日が来ていない月は数えない」を突くため joined_at を1日ずらす。この判定は
    # now.day と joined.day の大小で決まるので、実行日をそのまま使うと毎月 29〜31 日に
    # 落ちる (_months_ago が joined 側だけを 28 日へ丸めるため now.day > joined.day に
    # なってしまう)。現在時刻を固定して実行日から切り離す。
    c = _creator()
    membership = fc_services.join_free_tier(_member(), c)
    now = timezone.localtime().replace(year=2026, month=8, day=15)
    with _frozen_now(now):
        membership.joined_at = now - timedelta(days=1)
        assert membership.enrolled_months == 0
        membership.joined_at = now.replace(month=5)  # 3ヶ月前の応当日ちょうど
        assert membership.enrolled_months == 3
        # 応当日が来ていない月は数えない (3ヶ月前 + 1日 = まだ2ヶ月)
        membership.joined_at = now.replace(month=5) + timedelta(days=1)
        assert membership.enrolled_months == 2
        membership.joined_at = None
        assert membership.enrolled_months == 0


def test_loyalty_badge_steps(db):
    """勤続バッジ (#27 §3.1 Should)。1ヶ月未満はバッジなし、以降は達した段のラベル。"""
    c = _creator()
    membership = fc_services.join_free_tier(_member(), c)
    assert membership.loyalty_badge == ""  # 加入直後
    membership.joined_at = _months_ago(1)
    assert membership.loyalty_badge == "継続1ヶ月"
    membership.joined_at = _months_ago(5)
    assert membership.loyalty_badge == "継続3ヶ月"
    membership.joined_at = _months_ago(12)
    assert membership.loyalty_badge == "継続1年"
    membership.joined_at = _months_ago(30)
    assert membership.loyalty_badge == "継続2年"


@_PUBLIC_HOST
def test_fanclub_card_shows_loyalty_badge(http_client, db):
    c = _creator()
    m = _member()
    membership = fc_services.join_free_tier(m, c)
    membership.joined_at = _months_ago(7)
    membership.save(update_fields=["joined_at"])
    _login(http_client)
    res = http_client.get(f"/members/fanclub/{c.slug}/card/")
    assert "継続6ヶ月" in res.content.decode("utf-8")


# ---- 一覧 ----


@_PUBLIC_HOST
def test_fanclub_list_shows_joined_creators(http_client, db):
    ca, cb = _creator("circle-a"), _creator("circle-b", name="サークルB")
    m = _member()
    fc_services.join_free_tier(m, ca)
    _login(http_client)
    res = http_client.get("/members/fanclub/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "サークルA" in body and "No.00001" in body
    assert cb.name not in body  # 未加入は出さない


@_PUBLIC_HOST
def test_fanclub_list_hides_left_membership(http_client, db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    fc_services.leave(m, c)
    _login(http_client)
    res = http_client.get("/members/fanclub/")
    body = res.content.decode("utf-8")
    assert "まだ加入しているファンクラブがありません" in body


@_PUBLIC_HOST
def test_fanclub_list_shows_pending_tier_change(http_client, db):
    c = _creator()
    t1 = _paid_tier(c, level=1)
    t2 = _paid_tier(c, level=2, price_minor=1500, price_id="price_l2")
    m = _member()
    CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t2,
        status=MembershipStatus.ACTIVE,
        joined_at=timezone.now(),
        stripe_subscription_id="sub_x",
        pending_tier=t1,
        pending_tier_effective_at=timezone.now() + timedelta(days=10),
    )
    _login(http_client)
    res = http_client.get("/members/fanclub/")
    assert "へ変更予定" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_fanclub_list_requires_login(http_client, db):
    res = http_client.get("/members/fanclub/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_fanclub_list_backfills_missing_member_no(http_client, db):
    """機能実装前から在籍している行 (member_no=NULL) は一覧表示時に採番される。"""
    c = _creator()
    m = _member()
    membership = fc_services.join_free_tier(m, c)
    CreatorMembership.objects.filter(pk=membership.pk).update(member_no=None)
    _login(http_client)
    res = http_client.get("/members/fanclub/")
    assert res.status_code == 200
    membership.refresh_from_db()
    assert membership.member_no == 1


# ---- 会員証 ----


@_PUBLIC_HOST
def test_fanclub_card_shows_number_and_plan(http_client, db):
    c = _creator()
    t = _paid_tier(c, level=2, price_minor=1500, price_id="price_l2")
    m = _member()
    CreatorMembership.objects.create(
        member=m,
        creator=c,
        tier=t,
        status=MembershipStatus.ACTIVE,
        joined_at=_months_ago(3),
        current_period_end=timezone.now() + timedelta(days=10),
        stripe_subscription_id="sub_x",
        member_no=42,
    )
    _login(http_client)
    res = http_client.get(f"/members/fanclub/{c.slug}/card/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "00042" in body
    assert "ティア2" in body
    assert "3ヶ月" in body  # 継続月数
    assert "たろう" in body  # 会員証の名義


@_PUBLIC_HOST
def test_fanclub_card_redirects_when_not_a_member(http_client, db):
    c = _creator()
    _member()
    _login(http_client)
    res = http_client.get(f"/members/fanclub/{c.slug}/card/")
    assert res.status_code == 302 and res.url == "/members/fanclub/"


@_PUBLIC_HOST
def test_fanclub_card_redirects_after_leaving(http_client, db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    fc_services.leave(m, c)
    _login(http_client)
    res = http_client.get(f"/members/fanclub/{c.slug}/card/")
    assert res.status_code == 302 and res.url == "/members/fanclub/"


@_PUBLIC_HOST
def test_fanclub_card_does_not_leak_other_members_card(http_client, db):
    c = _creator()
    other = _member("other@example.com", nickname="はなこ")
    fc_services.join_free_tier(other, c)
    _member()  # 未加入の閲覧者
    _login(http_client)
    res = http_client.get(f"/members/fanclub/{c.slug}/card/")
    assert res.status_code == 302 and res.url == "/members/fanclub/"


@_PUBLIC_HOST
def test_fanclub_card_requires_login(http_client, db):
    c = _creator()
    res = http_client.get(f"/members/fanclub/{c.slug}/card/")
    assert res.status_code == 302 and res.url.startswith("/members/login/")


@_PUBLIC_HOST
def test_profile_links_to_fanclub(http_client, db):
    _member()
    _login(http_client)
    res = http_client.get("/members/")
    assert "/members/fanclub/" in res.content.decode("utf-8")
