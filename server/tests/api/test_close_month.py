# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase B B2: close_month 月次締め (金額算定/ロック/締め済み/警告/過月分割/日割り)。"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from django.utils import timezone

from billing.models import BillingPeriod, BroadcastCertificate, Invoice
from billing.services import BillingError, close_month
from medialib.models import Asset, AssetKind, CmCreative, CmGrid, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from sales.models import (
    AdContract,
    Advertiser,
    Agency,
    Airing,
    ContractKind,
    ContractStatus,
    Industry,
    Sponsorship,
    SpotOrder,
)
from scheduling.models import Series

Y, M = 2026, 6


def _aware(d, h=12):
    return timezone.make_aware(datetime.combine(d, time(h, 0)))


def _contract(agency=None):
    ind, _ = Industry.objects.get_or_create(code="auto", defaults={"name": "auto"})
    adv = Advertiser.objects.create(name="A社", industry=ind)
    return AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
        agency=agency,
        title="C",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 12, 31),
        status=ContractStatus.ACTIVE,
    )


def _spot(contract, channel, unit_price=50000):
    return SpotOrder.objects.create(
        contract=contract,
        channel=channel,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 12, 31),
        target_count=100,
        unit_seconds=15,
        unit_price=unit_price,
    )


def _cm(name="c"):
    a = Asset.objects.create(
        kind=AssetKind.CM, title=name, duration_ms=15000, normalize_status=NormalizeStatus.READY
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15)


def _airing(channel, so, cm, aired_at):
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=aired_at,
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.DONE,
        asset=cm.asset,
    )
    return Airing.objects.create(
        playout_event=ev,
        channel=channel,
        cm_asset=cm,
        spot_order=so,
        duration_ms=15000,
        aired_at=aired_at,
    )


def test_close_month_spot_direct_math(channel):
    c = _contract()  # 直販 (agency なし)
    so = _spot(c, channel, unit_price=50000)
    cm = _cm()
    for _ in range(3):
        _airing(channel, so, cm, _aware(date(Y, M, 15)))
    res = close_month(Y, M)
    assert res["closed"] is True
    inv = Invoice.objects.get(period__year=Y, period__month=M)
    assert inv.subtotal == 150000
    assert inv.commission_amount == 0  # 直販
    assert inv.tax_amount == 15000  # floor(150000 * 10%)
    assert inv.total == 165000
    # ロック + 確認書
    assert Airing.objects.filter(billed=True).count() == 3
    assert BroadcastCertificate.objects.filter(contract=c).exists()


def test_close_month_agency_commission_then_tax(channel):
    ag = Agency.objects.create(name="代理店", commission_rate=15)
    c = _contract(agency=ag)
    so = _spot(c, channel, unit_price=50000)
    cm = _cm()
    for _ in range(3):
        _airing(channel, so, cm, _aware(date(Y, M, 10)))
    close_month(Y, M)
    inv = Invoice.objects.get(period__year=Y, period__month=M)
    assert inv.subtotal == 150000
    assert inv.commission_amount == 22500  # floor(150000 * 15%)
    assert inv.tax_amount == 12750  # floor((150000-22500) * 10%)
    assert inv.total == 140250  # subtotal - commission + tax (CHECK)


def test_close_month_locks_and_already_closed(channel):
    c = _contract()
    so = _spot(c, channel)
    cm = _cm()
    _airing(channel, so, cm, _aware(date(Y, M, 5)))
    close_month(Y, M)
    assert BillingPeriod.objects.get(year=Y, month=M).closed_at is not None
    with pytest.raises(BillingError):
        close_month(Y, M)  # 二重締め禁止


def test_close_month_warns_on_unsent(channel):
    c = _contract()
    so = _spot(c, channel)
    cm = _cm()
    _airing(channel, so, cm, _aware(date(Y, M, 5)))
    # 当月に未返送 (SCHEDULED) の CM
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=_aware(date(Y, M, 20)),
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.SCHEDULED,
    )
    res = close_month(Y, M)  # force=False
    assert res["closed"] is False
    assert res["warnings"]
    assert not BillingPeriod.objects.filter(year=Y, month=M, closed_at__isnull=False).exists()
    # force で締められる
    res2 = close_month(Y, M, force=True)
    assert res2["closed"] is True


def test_close_month_past_month_split(channel):
    c = _contract()
    so = _spot(c, channel, unit_price=10000)
    cm = _cm()
    _airing(channel, so, cm, _aware(date(Y, M, 10)))  # 当月
    _airing(channel, so, cm, _aware(date(Y, 5, 28)))  # 前月 (billed=false で残存)
    close_month(Y, M)
    inv = Invoice.objects.get(period__year=Y, period__month=M)
    descs = [line.description for line in inv.lines.all()]
    assert any("当月分" in d for d in descs)
    assert any("前月分" in d for d in descs)


def test_sponsorship_full_month_proration(channel):
    c = _contract()
    s = Series.objects.create(channel=channel, title="レギュラー")
    Sponsorship.objects.create(contract=c, series=s, seconds_per_episode=30, monthly_fee=300000)
    res = close_month(Y, M)
    assert res["closed"] is True
    inv = Invoice.objects.get(period__year=Y, period__month=M, contract=c)
    # フル月有効 → monthly_fee そのまま (30/30 日)
    assert inv.subtotal == 300000
