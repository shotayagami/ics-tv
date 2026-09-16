# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""fanclub モデルの制約テスト (unique/Check/OneToOne)。"""

from __future__ import annotations

import pytest
from django.db import IntegrityError
from django.db.utils import DataError

from fanclub.models import (
    Creator,
    CreatorMembership,
    CreatorSeriesLink,
    CreatorTier,
    SlotContract,
)
from members.models import Member
from scheduling.models import Series

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _creator(slug="circle-a", **kw):
    return Creator.objects.create(name="サークルA", slug=slug, **kw)


def _series(channel, title="番組A"):
    return Series.objects.create(channel=channel, title=title)


def test_creator_slug_unique(db):
    _creator(slug="dup")
    with pytest.raises(IntegrityError):
        _creator(slug="dup")


def test_creator_tier_unique_creator_level(db):
    c = _creator()
    CreatorTier.objects.create(creator=c, level=0, name="無料", price_minor=None)
    with pytest.raises(IntegrityError):
        CreatorTier.objects.create(creator=c, level=0, name="無料2", price_minor=None)


def test_creator_tier_free_level_requires_null_price(db):
    c = _creator()
    with pytest.raises((IntegrityError, DataError)):
        CreatorTier.objects.create(creator=c, level=0, name="無料のはずが有料", price_minor=500)


def test_creator_tier_paid_level_allows_price(db):
    c = _creator()
    tier = CreatorTier.objects.create(creator=c, level=1, name="ベーシック", price_minor=500)
    assert tier.price_minor == 500


def test_creator_membership_unique_member_creator(db):
    c = _creator()
    tier = CreatorTier.objects.create(creator=c, level=0, name="無料", price_minor=None)
    m = _member()
    CreatorMembership.objects.create(member=m, creator=c, tier=tier, status="active")
    with pytest.raises(IntegrityError):
        CreatorMembership.objects.create(member=m, creator=c, tier=tier, status="active")


def test_creator_series_link_one_to_one(db, channel):
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    series = _series(channel)
    CreatorSeriesLink.objects.create(series=series, creator=c1)
    with pytest.raises(IntegrityError):
        CreatorSeriesLink.objects.create(series=series, creator=c2)


def test_creator_series_link_reverse_accessor(db, channel):
    c = _creator()
    series = _series(channel)
    CreatorSeriesLink.objects.create(series=series, creator=c)
    series.refresh_from_db()
    assert series.fanclub_link.creator_id == c.id


def test_slot_contract_basic_creation(db):
    c = _creator()
    contract = SlotContract.objects.create(
        creator=c,
        title="レギュラー枠",
        monthly_fee_minor=100_000,
        starts_on="2026-08-01",
        status="draft",
    )
    assert contract.status == "draft"
    assert contract.youtube_destination == "none"
