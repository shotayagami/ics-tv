# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase C C2: 提供(sponsorship)割付 優先1 + 提供秒数 quota + 提供主競合排除。"""

from __future__ import annotations

from datetime import date

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
    Placement,
    PlacementMatch,
    Sponsorship,
    SponsorshipMaterial,
    SpotOrder,
    SpotOrderBand,
    SpotOrderMaterial,
)
from scheduling.models import AdBreak, Program, ProgramType, Series
from scheduling.resolver import fill_break


def _ind(code="auto"):
    ind, _ = Industry.objects.get_or_create(code=code, defaults={"name": code})
    return ind


def _adv(name, code="auto"):
    return Advertiser.objects.create(
        name=name, industry=_ind(code), screening_status=ScreeningStatus.APPROVED
    )


def _cm(name, dur=15000):
    a = Asset.objects.create(
        kind=AssetKind.CM, title=name, duration_ms=dur, normalize_status=NormalizeStatus.READY
    )
    return CmCreative.objects.create(
        asset=a, advertiser=name, grid=CmGrid.G15, screening_status=ScreeningStatus.APPROVED
    )


def _contract(adv):
    return AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
        title="C",
        period_start=date(2020, 1, 1),
        period_end=date(2999, 1, 1),
        status=ContractStatus.ACTIVE,
    )


def _program(channel, asset_ready, series):
    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        series=series,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now,
        end_at=now + timezone.timedelta(hours=1),
        asset=asset_ready,
    )


def _break(prog, dur=15000):
    return AdBreak.objects.create(program=prog, offset_ms=600000, grid=CmGrid.G15, duration_ms=dur)


def _sponsorship(series, adv, seconds=15):
    c = AdContract.objects.create(
        kind=ContractKind.TIME,
        advertiser=adv,
        title="提供",
        period_start=date(2020, 1, 1),
        period_end=date(2999, 1, 1),
        status=ContractStatus.ACTIVE,
    )
    return Sponsorship.objects.create(
        contract=c, series=series, seconds_per_episode=seconds, monthly_fee=300000
    )


def _band_spot(channel, adv, cm):
    so = SpotOrder.objects.create(
        contract=_contract(adv),
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


def _now():
    return timezone.now()


def test_sponsorship_is_top_priority(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="提供番組")
    prog = _program(channel, asset_ready, s)
    br = _break(prog)
    sponsor = _adv("提供主")
    sp = _sponsorship(s, sponsor, seconds=15)
    cm_sp = _cm("sp")
    CmAdvertiserLink.objects.create(cm_asset=cm_sp, advertiser=sponsor)
    SponsorshipMaterial.objects.create(sponsorship=sp, seq=0, cm_asset=cm_sp)
    # 競合する band スポット (別広告主・別業種) も用意
    other = _adv("他社", code="food")
    cm_b = _cm("b")
    CmAdvertiserLink.objects.create(cm_asset=cm_b, advertiser=other)
    _band_spot(channel, other, cm_b)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    assert [c.asset_id for _it, c in pairs] == [cm_sp.asset_id]  # 提供が最優先
    pl = Placement.objects.get(ad_break_item=pairs[0][0])
    assert pl.match_kind == PlacementMatch.SPONSORSHIP
    assert pl.sponsorship_id == sp.id


def test_sponsorship_seconds_quota(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="提供番組")
    prog = _program(channel, asset_ready, s)
    br = _break(prog, dur=30000)  # 2 スロット
    sponsor = _adv("提供主")
    sp = _sponsorship(s, sponsor, seconds=15)  # 提供 15 秒 = 1 本ぶん
    cm_sp = _cm("sp")
    CmAdvertiserLink.objects.create(cm_asset=cm_sp, advertiser=sponsor)
    SponsorshipMaterial.objects.create(sponsorship=sp, seq=0, cm_asset=cm_sp)
    _cm("free")  # 残スロットはフリー

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    sponsor_placements = Placement.objects.filter(match_kind=PlacementMatch.SPONSORSHIP).count()
    assert sponsor_placements == 1  # quota 15s = 1 本まで
    assert len(pairs) == 2  # 提供1 + フリー1 で 30s 充填


def test_sponsor_conflict_blocks_same_industry_competitor(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="提供番組")
    prog = _program(channel, asset_ready, s)
    br = _break(prog)
    sponsor = _adv("提供主", code="auto")
    _sponsorship(s, sponsor, seconds=0)  # 提供枠自体は 0 (context だけ確立)
    # 同業種 (auto)・別広告主の band スポット → 提供主競合で弾かれる
    competitor = _adv("競合", code="auto")
    cm_c = _cm("c")
    CmAdvertiserLink.objects.create(cm_asset=cm_c, advertiser=competitor)
    _band_spot(channel, competitor, cm_c)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    assert pairs == []  # 競合排除でこの素材は入らず、フリー在庫も無いので空
    assert Placement.objects.count() == 0


def test_sponsor_own_additional_spot_allowed(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="提供番組")
    prog = _program(channel, asset_ready, s)
    br = _break(prog)
    sponsor = _adv("提供主", code="auto")
    _sponsorship(s, sponsor, seconds=0)
    # 提供主自身の追加スポットは許容
    cm_own = _cm("own")
    CmAdvertiserLink.objects.create(cm_asset=cm_own, advertiser=sponsor)
    _band_spot(channel, sponsor, cm_own)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    assert [c.asset_id for _it, c in pairs] == [cm_own.asset_id]  # 提供主自身は OK
    assert Placement.objects.get(ad_break_item=pairs[0][0]).match_kind == PlacementMatch.BAND


def test_program_credit_injected_on_head_event(channel, asset_ready):
    """提供番組を resolve すると番組頭 PLAY_ASSET の params に提供クレジットが載る (#6/casparcg §3.5)。"""
    from playout.models import PlayoutAction, PlayoutEvent
    from scheduling.resolver import resolve

    s = Series.objects.create(channel=channel, title="提供番組")
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now + timezone.timedelta(minutes=1),
        end_at=now + timezone.timedelta(minutes=1, hours=1),
        asset=asset_ready,
    )
    _sponsorship(s, _adv("提供主"), seconds=15)

    resolve(channel, now, now + timezone.timedelta(hours=2))
    head = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_ASSET)
        .order_by("scheduled_at")
        .first()
    )
    assert head is not None
    assert head.params.get("cg_sponsor") == "提供主"
    assert head.params.get("cg_template") == "credit/sponsor"


def test_program_credit_none_without_sponsorship(channel, asset_ready):
    p = ContractConstraintProvider()
    s = Series.objects.create(channel=channel, title="非提供")
    prog = Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=timezone.now(),
        end_at=timezone.now() + timezone.timedelta(hours=1),
        asset=asset_ready,
    )
    assert p.program_credit(prog, timezone.now()) is None
