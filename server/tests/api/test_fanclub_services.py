# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""fanclub.services のエンタイトルメント解決 + join/leave。"""

from __future__ import annotations

import pytest

from fanclub import services
from fanclub.models import Creator, CreatorMembership, CreatorSeriesLink, CreatorTier
from members.models import Member
from scheduling.models import Series

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _creator(slug="circle-a", status="active"):
    return Creator.objects.create(name="サークルA", slug=slug, status=status)


def _series(channel, title="番組A"):
    return Series.objects.create(channel=channel, title=title)


# ---- member_level / can_view_level ----


def test_member_level_none_when_anonymous_or_no_creator(db):
    c = _creator()
    assert services.member_level(None, c) is None
    assert services.member_level(_member(), None) is None


def test_member_level_returns_active_tier_level(db):
    c = _creator()
    tier = services.ensure_free_tier(c)
    m = _member()
    services.join(m, c, tier)
    assert services.member_level(m, c) == 0


def test_member_level_none_after_leave(db):
    c = _creator()
    tier = services.ensure_free_tier(c)
    m = _member()
    services.join(m, c, tier)
    services.leave(m, c)
    assert services.member_level(m, c) is None


def test_can_view_level_null_required_always_true(db):
    assert services.can_view_level(None, None, None) is True


def test_can_view_level_boundary(db):
    c = _creator()
    tier = services.ensure_free_tier(c)
    m = _member()
    # 未加入は level0要求でも不可
    assert services.can_view_level(0, m, c) is False
    services.join(m, c, tier)
    # 加入後は level0要求を満たす
    assert services.can_view_level(0, m, c) is True
    # 有料ティア要求(level1)は満たさない
    assert services.can_view_level(1, m, c) is False


def test_can_view_level_creator_none_is_false_for_required(db):
    assert services.can_view_level(0, _member(), None) is False


def test_can_view_level_suspended_creator_fails_closed(db):
    c = _creator(status="suspended")
    tier = CreatorTier.objects.create(creator=c, level=0, name="無料", price_minor=None)
    m = _member()
    CreatorMembership.objects.create(
        member=m, creator=c, tier=tier, status="active", joined_at=None
    )
    # member は在籍データを持つが creator が停止中なのでフェイルクローズ
    assert services.can_view_level(0, m, c) is False


# ---- creator_for_series / member_levels_by_series ----


def test_creator_for_series(db, channel):
    c = _creator()
    s = _series(channel)
    assert services.creator_for_series(s) is None
    CreatorSeriesLink.objects.create(series=s, creator=c)
    assert services.creator_for_series(s).id == c.id


def test_creator_for_series_none_input(db):
    assert services.creator_for_series(None) is None


def test_member_levels_by_series_bulk(db, channel, django_assert_num_queries):
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    t1 = services.ensure_free_tier(c1)
    services.ensure_free_tier(c2)
    s1 = _series(channel, "番組1")
    s2 = _series(channel, "番組2")
    s3 = _series(channel, "番組3(未紐付)")
    CreatorSeriesLink.objects.create(series=s1, creator=c1)
    CreatorSeriesLink.objects.create(series=s2, creator=c2)
    m = _member()
    services.join(m, c1, t1)

    with django_assert_num_queries(2):
        result = services.member_levels_by_series(m, [s1.id, s2.id, s3.id])

    assert result == {s1.id: 0, s2.id: None}
    assert s3.id not in result


def test_member_levels_by_series_empty_input(db):
    assert services.member_levels_by_series(_member(), []) == {}


def test_member_levels_by_series_anonymous(db, channel):
    c = _creator()
    services.ensure_free_tier(c)
    s = _series(channel)
    CreatorSeriesLink.objects.create(series=s, creator=c)
    assert services.member_levels_by_series(None, [s.id]) == {s.id: None}


# ---- fc_gate_reason ----


def test_fc_gate_reason_all_branches(db):
    c = _creator()
    tier = services.ensure_free_tier(c)
    m = _member()

    assert services.fc_gate_reason(None, None, None) == ""
    assert services.fc_gate_reason(0, None, c) == "login"
    assert services.fc_gate_reason(0, m, c) == "fc_join"
    services.join(m, c, tier)
    assert services.fc_gate_reason(0, m, c) == ""
    # 有料ティア要求は Phase A では常に fc_unavailable (JOINABLE_LEVELS 外)
    assert services.fc_gate_reason(1, m, c) == "fc_unavailable"
    assert services.fc_gate_reason(0, m, None) == "fc_unavailable"


def test_fc_gate_reason_true_for_manually_granted_paid_level(db):
    """加入導線の可否 (JOINABLE_LEVELS) と閲覧可否ロジックは分離されている。

    運用上 level1 の CreatorMembership が直接作られた場合 (studio 側の手動付与等)、
    閲覧自体は許可される (can_view_level はレベル比較のみで JOINABLE_LEVELS を見ない)。
    """
    c = _creator()
    tier = CreatorTier.objects.create(creator=c, level=1, name="ベーシック", price_minor=500)
    m = _member()
    CreatorMembership.objects.create(member=m, creator=c, tier=tier, status="active")
    assert services.fc_gate_reason(1, m, c) == ""


# ---- ensure_free_tier / join / leave ----


def test_ensure_free_tier_idempotent(db):
    c = _creator()
    t1 = services.ensure_free_tier(c)
    t2 = services.ensure_free_tier(c)
    assert t1.pk == t2.pk
    assert CreatorTier.objects.filter(creator=c, level=0).count() == 1


def test_join_free_tier_creates_membership(db):
    c = _creator()
    services.ensure_free_tier(c)
    m = _member()
    membership = services.join_free_tier(m, c)
    assert membership.status == "active"
    assert membership.tier.level == 0
    assert membership.joined_at is not None


def test_join_free_tier_without_tier_raises(db):
    c = _creator()
    m = _member()
    with pytest.raises(services.TierNotJoinableError):
        services.join_free_tier(m, c)


def test_join_rejects_level_outside_joinable(db):
    c = _creator()
    tier = CreatorTier.objects.create(creator=c, level=1, name="ベーシック", price_minor=500)
    m = _member()
    with pytest.raises(services.TierNotJoinableError):
        services.join(m, c, tier)


def test_join_rejects_inactive_tier(db):
    c = _creator()
    tier = CreatorTier.objects.create(
        creator=c, level=0, name="無料", price_minor=None, is_active=False
    )
    m = _member()
    with pytest.raises(services.TierNotJoinableError):
        services.join(m, c, tier)


def test_rejoin_reuses_existing_row(db):
    c = _creator()
    services.ensure_free_tier(c)
    m = _member()
    services.join_free_tier(m, c)
    services.leave(m, c)
    services.join_free_tier(m, c)
    assert CreatorMembership.objects.filter(member=m, creator=c).count() == 1
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == "active"
    assert membership.left_at is None


def test_leave_sets_left_status(db):
    c = _creator()
    services.ensure_free_tier(c)
    m = _member()
    services.join_free_tier(m, c)
    services.leave(m, c)
    membership = CreatorMembership.objects.get(member=m, creator=c)
    assert membership.status == "left"
    assert membership.left_at is not None


def test_leave_noop_when_not_a_member(db):
    c = _creator()
    m = _member()
    services.leave(m, c)  # 例外を投げない
    assert CreatorMembership.objects.filter(member=m, creator=c).count() == 0
