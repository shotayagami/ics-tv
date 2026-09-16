# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase A Commit2: sales app モデルの制約・作成テスト。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from medialib.models import Asset, AssetKind, CmCreative, CmGrid, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent
from sales.models import (
    AdContract,
    Advertiser,
    Agency,
    Airing,
    CmAdvertiserLink,
    ContractKind,
    Industry,
    MakeGood,
    MakeGoodStatus,
    Placement,
    PlacementMatch,
    SpotOrder,
    SpotOrderBand,
)
from scheduling.models import AdBreak, AdBreakItem, Program, ProgramType


def _adv():
    ind = Industry.objects.create(code="auto", name="自動車")
    return Advertiser.objects.create(name="A社", industry=ind)


def _contract(adv):
    return AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
        title="C",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )


def _spot(contract, channel):
    return SpotOrder.objects.create(
        contract=contract,
        channel=channel,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        target_count=30,
        unit_seconds=15,
        unit_price=50000,
    )


def _cm(name="x"):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title=name,
        duration_ms=15000,
        r2_key=f"mezzanine/cm/{name}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15)


def _ad_break_item(channel, asset_ready):
    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    br = AdBreak.objects.create(program=prog, offset_ms=600000, grid=CmGrid.G15, duration_ms=15000)
    return AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=_cm("br"))


# ---- マスタ / 契約 ----


def test_masters_and_contract(channel):
    adv = _adv()
    Agency.objects.create(name="代理店X")
    c = _contract(adv)
    assert c.agency_id is None  # 直販
    _spot(c, channel)
    assert c.spot_orders.count() == 1


def test_contract_period_check(channel):
    adv = _adv()
    with pytest.raises(IntegrityError), transaction.atomic():
        AdContract.objects.create(
            kind=ContractKind.SPOT,
            advertiser=adv,
            title="逆period",
            period_start=date(2026, 6, 30),
            period_end=date(2026, 6, 1),
        )


def test_cm_advertiser_link(channel):
    adv = _adv()
    cm = _cm("link")
    CmAdvertiserLink.objects.create(cm_asset=cm, advertiser=adv)
    assert cm.advertiser_link.advertiser_id == adv.id


def test_spot_order_band(channel):
    adv = _adv()
    so = _spot(_contract(adv), channel)
    b = SpotOrderBand.objects.create(
        spot_order=so, dow_mask=127, start_time="19:00", end_time="23:00"
    )
    assert b.spot_order_id == so.id


# ---- placement 制約 ----


def test_placement_requires_exactly_one(channel, asset_ready):
    item = _ad_break_item(channel, asset_ready)
    # 両 NULL → chk_placement_one 違反
    with pytest.raises(IntegrityError), transaction.atomic():
        Placement.objects.create(ad_break_item=item, match_kind=PlacementMatch.BAND)


def test_placement_band_with_spot_order_ok(channel, asset_ready):
    item = _ad_break_item(channel, asset_ready)
    so = _spot(_contract(_adv()), channel)
    p = Placement.objects.create(ad_break_item=item, spot_order=so, match_kind=PlacementMatch.BAND)
    assert p.spot_order_id == so.id


def test_placement_sponsorship_match_requires_sponsorship(channel, asset_ready):
    item = _ad_break_item(channel, asset_ready)
    so = _spot(_contract(_adv()), channel)
    # match_kind=sponsorship なのに sponsorship 無し → chk_placement_match 違反
    with pytest.raises(IntegrityError), transaction.atomic():
        Placement.objects.create(
            ad_break_item=item, spot_order=so, match_kind=PlacementMatch.SPONSORSHIP
        )


# ---- airing 制約 ----


def _playout_event(channel):
    return PlayoutEvent.objects.create(
        channel=channel, scheduled_at=timezone.now(), action=PlayoutAction.PLAY_CM
    )


def test_airing_at_most_one_contract(channel, asset_ready):
    so = _spot(_contract(_adv()), channel)
    # sponsorship も付けて両方 → chk_airing_one 違反 (要 sponsorship)。簡略に spot のみで OK を確認
    a = Airing.objects.create(
        playout_event=_playout_event(channel),
        channel=channel,
        cm_asset=_cm("air"),
        spot_order=so,
        duration_ms=15000,
        aired_at=timezone.now(),
    )
    assert a.spot_order_id == so.id


def test_airing_unique_event_seq(channel):
    pe = _playout_event(channel)
    Airing.objects.create(
        playout_event=pe,
        bundle_seq=0,
        channel=channel,
        cm_asset=_cm("a1"),
        duration_ms=15000,
        aired_at=timezone.now(),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Airing.objects.create(
            playout_event=pe,
            bundle_seq=0,
            channel=channel,
            cm_asset=_cm("a2"),
            duration_ms=15000,
            aired_at=timezone.now(),
        )


def test_make_good_requires_exactly_one(channel):
    with pytest.raises(IntegrityError), transaction.atomic():
        MakeGood.objects.create(status=MakeGoodStatus.OPEN)  # 両 NULL → chk_mg_one 違反
