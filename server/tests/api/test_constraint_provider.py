# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase A Commit3b: ContractConstraintProvider (指定番組/線引き/考査/業種同一枠/残本数)。

provider を直接 fill_break に渡して契約駆動割付を検証する。
"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from medialib.models import (
    Asset,
    AssetKind,
    CmCreative,
    CmGrid,
    NormalizeStatus,
    ScreeningStatus,
)
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
    SpotOrder,
    SpotOrderBand,
    SpotOrderMaterial,
    SpotOrderProgram,
)
from scheduling.models import AdBreak, Program, ProgramType, Series
from scheduling.resolver import fill_break


def _cm(name, dur=15000, screening=ScreeningStatus.APPROVED, aired=0):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title=name,
        duration_ms=dur,
        r2_key=f"mezzanine/cm/{name}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(
        asset=a, advertiser=name, grid=CmGrid.G15, screening_status=screening, aired_count=aired
    )


def _advertiser(name, screening=ScreeningStatus.APPROVED, industry_code="auto"):
    ind, _ = Industry.objects.get_or_create(code=industry_code, defaults={"name": industry_code})
    return Advertiser.objects.create(name=name, industry=ind, screening_status=screening)


def _link(cm, advertiser):
    CmAdvertiserLink.objects.create(cm_asset=cm, advertiser=advertiser)


def _spot_order(channel, advertiser, target=10):
    c = AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=advertiser,
        title="C",
        period_start=date(2020, 1, 1),
        period_end=date(2999, 1, 1),
        status=ContractStatus.ACTIVE,
    )
    return SpotOrder.objects.create(
        contract=c,
        channel=channel,
        period_start=date(2020, 1, 1),
        period_end=date(2999, 1, 1),
        target_count=target,
        unit_seconds=15,
        unit_price=50000,
    )


def _program(channel, asset_ready, series=None, dur_ms=3_600_000):
    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        series=series,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=now,
        end_at=now + timedelta(milliseconds=dur_ms),
        asset=asset_ready,
    )


def _break(prog, dur=15000):
    return AdBreak.objects.create(program=prog, offset_ms=600000, grid=CmGrid.G15, duration_ms=dur)


def _now():
    return timezone.now()


def test_program_match_places_contract_cm(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="レギュラー")
    prog = _program(channel, asset_ready, series=s)
    br = _break(prog)
    adv = _advertiser("A社")
    cm = _cm("cmA")
    _link(cm, adv)
    so = _spot_order(channel, adv)
    SpotOrderProgram.objects.create(spot_order=so, series=s)
    SpotOrderMaterial.objects.create(spot_order=so, seq=0, cm_asset=cm)
    _cm("free")  # 契約外も用意 (こちらが選ばれないことを確認)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    assert [c.asset_id for _it, c in pairs] == [cm.asset_id]  # 契約 CM 優先
    pl = Placement.objects.get(ad_break_item=pairs[0][0])
    assert pl.match_kind == PlacementMatch.PROGRAM
    assert pl.spot_order_id == so.id


def test_band_match_places_contract_cm(channel, asset_ready):
    prog = _program(channel, asset_ready)
    br = _break(prog)
    adv = _advertiser("B社", industry_code="food")
    cm = _cm("cmB")
    _link(cm, adv)
    so = _spot_order(channel, adv)
    # 全時間帯・全曜日の band
    SpotOrderBand.objects.create(spot_order=so, dow_mask=127, start_time="00:00", end_time="23:59")
    SpotOrderMaterial.objects.create(spot_order=so, seq=0, cm_asset=cm)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    assert [c.asset_id for _it, c in pairs] == [cm.asset_id]
    assert Placement.objects.get(ad_break_item=pairs[0][0]).match_kind == PlacementMatch.BAND


def test_screening_gate_blocks_unapproved_advertiser(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="レギュラー")
    prog = _program(channel, asset_ready, series=s)
    br = _break(prog)
    adv = _advertiser("未考査社", screening=ScreeningStatus.PENDING)  # 業態考査 未
    cm = _cm("cmNG")
    _link(cm, adv)
    so = _spot_order(channel, adv)
    SpotOrderProgram.objects.create(spot_order=so, series=s)
    SpotOrderMaterial.objects.create(spot_order=so, seq=0, cm_asset=cm)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    # 考査未通過は「契約割付 (placement)」を阻むのみ (S8: フリー在庫としては使える)。
    # → 枠は埋まるが contract placement は作られない
    assert len(pairs) == 1
    assert Placement.objects.count() == 0


def test_industry_conflict_same_break(channel, asset_ready):
    prog = _program(channel, asset_ready)
    br = _break(prog, dur=30000)  # 2 スロット
    adv1 = _advertiser("A1", industry_code="auto")
    adv2 = _advertiser("A2", industry_code="auto")  # 同業種
    cm1, cm2 = _cm("c1"), _cm("c2")
    _link(cm1, adv1)
    _link(cm2, adv2)
    so1 = _spot_order(channel, adv1)
    so2 = _spot_order(channel, adv2)
    for so, cm in ((so1, cm1), (so2, cm2)):
        SpotOrderBand.objects.create(
            spot_order=so, dow_mask=127, start_time="00:00", end_time="23:59"
        )
        SpotOrderMaterial.objects.create(spot_order=so, seq=0, cm_asset=cm)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    # 同業種は同一枠に 1 本のみ (2 本目は accept で弾かれ、フリー在庫も無いので 1 本)
    assert len(pairs) == 1


def test_remaining_caps_total(channel, asset_ready):
    prog = _program(channel, asset_ready)
    br = _break(prog, dur=30000)  # 2 スロット
    adv = _advertiser("Cap社")
    cm = _cm("cap")
    _link(cm, adv)
    so = _spot_order(channel, adv, target=1)  # 残本数 1
    SpotOrderBand.objects.create(spot_order=so, dow_mask=127, start_time="00:00", end_time="23:59")
    SpotOrderMaterial.objects.create(spot_order=so, seq=0, cm_asset=cm)

    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    # target_count=1 なので 1 本だけ (同一素材重複も無し)。残スロットはフリー無しで空
    assert len(pairs) == 1


def test_free_fallback_when_no_contract(channel, asset_ready):
    prog = _program(channel, asset_ready)
    br = _break(prog)
    free = _cm("free")
    pairs = fill_break(br, _now(), provider=ContractConstraintProvider())
    assert [c.asset_id for _it, c in pairs] == [free.asset_id]
    assert Placement.objects.count() == 0  # 契約外は placement 作らない


def test_refill_window_skips_break_with_done_event(channel, asset_ready):
    """DONE を含む枠は不変 (S11 安全ガード)。clear されないので resolve も発火しない。"""
    from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
    from sales.tasks import refill_window
    from scheduling.models import AdBreakItem

    prog = _program(channel, asset_ready)
    br = _break(prog)
    cm = _cm("c")
    item = AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=cm)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=_now(),
        action=PlayoutAction.PLAY_CM,
        ad_break_item=item,
        status=PlayoutStatus.DONE,  # 送出済
    )
    res = refill_window(channel.id)
    assert res["cleared"] == 0
    assert AdBreakItem.objects.filter(pk=item.id).exists()  # 不変
