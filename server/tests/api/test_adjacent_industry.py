# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 隣接枠の業種競合 (S4補足): 放送順で直前ブレーク末尾と枠先頭が同業種にならない。"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from medialib.models import Asset, AssetKind, CmCreative, CmGrid, NormalizeStatus, ScreeningStatus
from sales.constraints import ContractConstraintProvider
from sales.models import (
    AdContract,
    Advertiser,
    CmAdvertiserLink,
    ContractKind,
    ContractStatus,
    Industry,
    SpotOrder,
    SpotOrderBand,
    SpotOrderMaterial,
)
from scheduling.models import AdBreak, Program, ProgramType
from scheduling.resolver import fill_break


def _ind(code):
    ind, _ = Industry.objects.get_or_create(code=code, defaults={"name": code})
    return ind


def _adv(name, code):
    return Advertiser.objects.create(
        name=name, industry=_ind(code), screening_status=ScreeningStatus.APPROVED
    )


def _cm(name):
    a = Asset.objects.create(
        kind=AssetKind.CM, title=name, duration_ms=15000, normalize_status=NormalizeStatus.READY
    )
    return CmCreative.objects.create(
        asset=a, advertiser=name, grid=CmGrid.G15, screening_status=ScreeningStatus.APPROVED
    )


def _band_spot(channel, adv, cm):
    so = SpotOrder.objects.create(
        contract=AdContract.objects.create(
            kind=ContractKind.SPOT,
            advertiser=adv,
            title="C",
            period_start=date(2020, 1, 1),
            period_end=date(2999, 1, 1),
            status=ContractStatus.ACTIVE,
        ),
        channel=channel,
        period_start=date(2020, 1, 1),
        period_end=date(2999, 1, 1),
        target_count=100,
        unit_seconds=15,
        unit_price=50000,
    )
    SpotOrderBand.objects.create(spot_order=so, dow_mask=127, start_time="00:00", end_time="23:59")
    SpotOrderMaterial.objects.create(spot_order=so, seq=0, cm_asset=cm)
    return so


def _program(channel, asset_ready, start=None):
    now = start or timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )


def _break(prog, offset):
    return AdBreak.objects.create(
        program=prog, offset_ms=offset, grid=CmGrid.G15, duration_ms=15000
    )


def _link(cm, adv):
    CmAdvertiserLink.objects.create(cm_asset=cm, advertiser=adv)


def test_adjacent_same_industry_blocked_within_program(channel, asset_ready):
    prog = _program(channel, asset_ready)
    br1, br2 = _break(prog, 600000), _break(prog, 1200000)
    auto1 = _adv("A1", "auto")
    cm1 = _cm("c1")
    _link(cm1, auto1)
    _band_spot(channel, auto1, cm1)
    # br2 用に別広告主だが同業種 (auto) の素材のみ
    auto2 = _adv("A2", "auto")
    cm2 = _cm("c2")
    _link(cm2, auto2)
    _band_spot(channel, auto2, cm2)

    p = ContractConstraintProvider()
    fill_break(br1, timezone.now(), provider=p)  # br1 = auto
    pairs2 = fill_break(br2, timezone.now(), provider=p)
    assert pairs2 == []  # br2 先頭は直前(auto)と連続 → 弾かれ、他在庫無しで空


def test_adjacent_different_industry_allowed(channel, asset_ready):
    prog = _program(channel, asset_ready)
    br1, br2 = _break(prog, 600000), _break(prog, 1200000)
    food = _adv("F", "food")
    cm1 = _cm("c1")
    _link(cm1, food)
    _band_spot(channel, food, cm1)
    auto = _adv("A", "auto")
    cm2 = _cm("c2")
    _link(cm2, auto)
    _band_spot(channel, auto, cm2)

    p = ContractConstraintProvider()
    fill_break(br1, timezone.now(), provider=p)  # br1 = food
    pairs2 = fill_break(br2, timezone.now(), provider=p)
    assert [c.asset_id for _it, c in pairs2] == [cm2.asset_id]  # 業種が違えば OK


def test_adjacent_across_program_boundary(channel, asset_ready):
    now = timezone.now()
    prog_a = _program(channel, asset_ready, start=now)
    prog_b = _program(channel, asset_ready, start=now + timedelta(hours=2))
    br_a = _break(prog_a, 600000)
    br_b = _break(prog_b, 600000)  # prog_b 頭のブレーク
    auto1 = _adv("A1", "auto")
    cm_a = _cm("a")
    _link(cm_a, auto1)
    _band_spot(channel, auto1, cm_a)
    auto2 = _adv("A2", "auto")
    cm_b = _cm("b")
    _link(cm_b, auto2)
    _band_spot(channel, auto2, cm_b)

    p = ContractConstraintProvider()
    fill_break(br_a, now, provider=p)  # prog_a 末尾 = auto
    pairs_b = fill_break(br_b, now + timedelta(hours=2), provider=p)
    assert pairs_b == []  # 番組境界をまたいで auto 連続 → 弾く
