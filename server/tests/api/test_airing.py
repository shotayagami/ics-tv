# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase A Commit4: 放確台帳 (record_airing / reconcile / detect_missed / apply_result フック)。"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from medialib.models import (
    Asset,
    AssetKind,
    CmBundle,
    CmBundleItem,
    CmCreative,
    CmGrid,
    NormalizeStatus,
)
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from sales.models import (
    AdContract,
    Advertiser,
    Airing,
    ContractKind,
    ContractStatus,
    Industry,
    MakeGood,
    Placement,
    PlacementMatch,
    SpotOrder,
)
from sales.tasks import detect_missed_airings, reconcile_airings, record_airing
from scheduling.models import AdBreak, AdBreakItem, Program, ProgramType


def _cm(name, dur=15000):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title=name,
        duration_ms=dur,
        r2_key=f"mezzanine/cm/{name}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15)


def _spot_order(channel):
    ind, _ = Industry.objects.get_or_create(code="auto", defaults={"name": "auto"})
    adv = Advertiser.objects.create(name="A社", industry=ind)
    c = AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
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
        target_count=10,
        unit_seconds=15,
        unit_price=50000,
    )


def _item_with_placement(channel, asset_ready, cm, so):
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
    Placement.objects.create(ad_break_item=item, spot_order=so, match_kind=PlacementMatch.BAND)
    return item, prog


def _done_cm(channel, cm, item=None, program=None, actual=True):
    now = timezone.now()
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now,
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.DONE,
        asset=cm.asset,
        ad_break_item=item,
        program=program,
        actual_at=now if actual else None,
        params={"duration_ms": cm.asset.duration_ms},
    )


def test_record_airing_with_placement_sets_contract(channel, asset_ready):
    cm = _cm("c")
    so = _spot_order(channel)
    item, prog = _item_with_placement(channel, asset_ready, cm, so)
    ev = _done_cm(channel, cm, item=item, program=prog)
    record_airing(ev.id)
    a = Airing.objects.get(playout_event=ev, bundle_seq=0)
    assert a.spot_order_id == so.id  # placement 経由で契約帰属確定
    assert a.cm_asset_id == cm.asset_id
    assert a.program_title == "番組"  # スナップショット
    assert a.duration_ms == 15000
    assert a.aired_at_estimated is False


def test_record_airing_no_placement_is_contract_free(channel):
    cm = _cm("free")
    ev = _done_cm(channel, cm)  # ad_break_item なし
    record_airing(ev.id)
    a = Airing.objects.get(playout_event=ev)
    assert a.spot_order_id is None and a.sponsorship_id is None  # 契約外 (両 NULL)


def test_record_airing_estimates_aired_at_when_actual_null(channel):
    cm = _cm("c")
    ev = _done_cm(channel, cm, actual=False)  # actual_at NULL
    record_airing(ev.id)
    a = Airing.objects.get(playout_event=ev)
    assert a.aired_at_estimated is True
    assert a.aired_at == ev.scheduled_at  # scheduled_at で補完


def test_record_airing_idempotent(channel):
    cm = _cm("c")
    ev = _done_cm(channel, cm)
    record_airing(ev.id)
    record_airing(ev.id)  # 再送
    assert Airing.objects.filter(playout_event=ev).count() == 1  # (event,seq) UNIQUE


def test_record_airing_bundle_expands_per_cm(channel):
    bundle = CmBundle.objects.create(name="reel")
    cm0, cm1 = _cm("b0", 15000), _cm("b1", 20000)
    CmBundleItem.objects.create(cm_bundle=bundle, seq=0, cm_asset=cm0)
    CmBundleItem.objects.create(cm_bundle=bundle, seq=1, cm_asset=cm1)
    now = timezone.now()
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now,
        action=PlayoutAction.PLAY_CM_BUNDLE,
        status=PlayoutStatus.DONE,
        cm_bundle=bundle,
        actual_at=now,
    )
    record_airing(ev.id)
    airings = list(Airing.objects.filter(playout_event=ev).order_by("bundle_seq"))
    assert [a.bundle_seq for a in airings] == [0, 1]  # CmBundleItem 1 本ごと
    # 2 本目の aired_at は先頭尺ぶんオフセット
    assert airings[1].aired_at == now + timedelta(milliseconds=15000)


def test_reconcile_creates_missing_airing(channel):
    cm = _cm("c")
    ev = _done_cm(channel, cm)  # airing 未作成 (broker 不達想定)
    assert not Airing.objects.filter(playout_event=ev).exists()
    reconcile_airings()
    assert Airing.objects.filter(playout_event=ev).exists()


def test_detect_missed_creates_make_good(channel, asset_ready):
    cm = _cm("c")
    so = _spot_order(channel)
    item, _ = _item_with_placement(channel, asset_ready, cm, so)
    now = timezone.now()
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now,
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.FAILED,  # 欠送
        asset=cm.asset,
        ad_break_item=item,
    )
    detect_missed_airings()
    mg = MakeGood.objects.get(missed_playout_event=ev)
    assert mg.spot_order_id == so.id
    assert mg.status == "open"
    # 冪等 (再実行で重複起票しない)
    detect_missed_airings()
    assert MakeGood.objects.filter(missed_playout_event=ev).count() == 1


def test_apply_result_enqueues_record_airing_on_first_done(
    channel, monkeypatch, django_capture_on_commit_callbacks
):
    from playout import grpc_service

    calls = []
    monkeypatch.setattr(grpc_service, "_enqueue_record_airing", lambda eid: calls.append(eid))
    cm = _cm("c")
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=timezone.now(),
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.SCHEDULED,
        asset=cm.asset,
    )
    with django_capture_on_commit_callbacks(execute=True):
        grpc_service.apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")
    assert calls == [ev.id]  # 初回 DONE で record_airing を on_commit 発火
