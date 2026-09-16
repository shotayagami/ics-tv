# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 営業 UI (ui-sales.md §3/§4): 線引きエディタ・割付ビュー・手動差し替え。"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from medialib.models import Asset, AssetKind, CmCreative, CmGrid, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from sales.models import (
    AdContract,
    Advertiser,
    ContractKind,
    ContractStatus,
    Industry,
    SpotOrder,
    SpotOrderBand,
    SpotOrderProgram,
)
from scheduling.models import AdBreak, AdBreakItem, Program, ProgramType, Series


def _order(channel):
    ind = Industry.objects.create(code="auto", name="自動車")
    adv = Advertiser.objects.create(name="A社", industry=ind)
    c = AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
        title="夏キャンペーン",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        status=ContractStatus.ACTIVE,
    )
    return SpotOrder.objects.create(
        contract=c,
        channel=channel,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        target_count=100,
        unit_seconds=15,
        unit_price=30000,
    )


def _cm(name="c"):
    a = Asset.objects.create(
        kind=AssetKind.CM, title=name, duration_ms=15000, normalize_status=NormalizeStatus.READY
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15)


def _item(channel, asset_ready, cm, status=PlayoutStatus.SCHEDULED):
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
    item = AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=cm)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now,
        action=PlayoutAction.PLAY_CM,
        status=status,
        asset=cm.asset,
        ad_break_item=item,
    )
    return prog, item


# ---- 線引きエディタ ----


def test_band_editor_requires_staff(http_client, channel, db):
    so = _order(channel)
    assert http_client.get(f"/sales/orders/{so.id}/bands/").status_code == 302


def test_add_and_delete_band(staff_client, channel, db):
    so = _order(channel)
    res = staff_client.post(
        f"/sales/orders/{so.id}/bands/add/",
        {"dow": ["0", "2", "4"], "start_time": "19:00", "end_time": "23:00"},
    )
    assert res.status_code == 200
    band = SpotOrderBand.objects.get(spot_order=so)
    assert band.dow_mask == 0b0010101  # 月(0)+水(2)+金(4)
    res2 = staff_client.post(f"/sales/bands/{band.id}/delete/")
    assert res2.status_code == 200
    assert not SpotOrderBand.objects.filter(pk=band.id).exists()


def test_add_band_rejects_reversed_time(staff_client, channel, db):
    so = _order(channel)
    res = staff_client.post(
        f"/sales/orders/{so.id}/bands/add/",
        {"dow": ["0"], "start_time": "23:00", "end_time": "19:00"},
    )
    assert res.status_code == 400
    assert not SpotOrderBand.objects.exists()


def test_add_program_same_channel_only(staff_client, channel, db):
    so = _order(channel)
    s = Series.objects.create(channel=channel, title="朝のニュース")
    res = staff_client.post(f"/sales/orders/{so.id}/programs/add/", {"series_id": s.id})
    assert res.status_code == 200
    assert SpotOrderProgram.objects.filter(spot_order=so, series=s).exists()


# ---- 割付ビュー / 手動差し替え ----


def test_allocation_view_renders(staff_client, channel, asset_ready, db):
    cm = _cm("x")
    _item(channel, asset_ready, cm)
    res = staff_client.get(f"/sales/allocation/?channel={channel.id}")
    assert res.status_code == 200
    assert "割付ビュー" in res.content.decode("utf-8")


def test_swap_scheduled_item(staff_client, channel, asset_ready, db):
    cm = _cm("old")
    new = _cm("new")
    _prog, item = _item(channel, asset_ready, cm, status=PlayoutStatus.SCHEDULED)
    res = staff_client.post(f"/sales/items/{item.id}/swap/", {"cm_asset_id": new.asset_id})
    assert res.status_code == 200
    item.refresh_from_db()
    assert item.cm_asset_id == new.asset_id
    ev = PlayoutEvent.objects.get(ad_break_item=item)
    assert ev.asset_id == new.asset_id  # イベントの素材参照も同期
    assert ev.params["clip"] == f"cm/{new.asset_id}"


def test_swap_blocked_when_done(staff_client, channel, asset_ready, db):
    cm = _cm("old")
    new = _cm("new")
    _prog, item = _item(channel, asset_ready, cm, status=PlayoutStatus.DONE)
    res = staff_client.post(f"/sales/items/{item.id}/swap/", {"cm_asset_id": new.asset_id})
    assert res.status_code == 409  # 送出済は差替不可
    item.refresh_from_db()
    assert item.cm_asset_id == cm.asset_id
