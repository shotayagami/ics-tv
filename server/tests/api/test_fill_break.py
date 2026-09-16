# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase A Commit3a: fill_break 内部拡張 (残尺ベース / (item,cm) ペア / ad_break_item FK)。

provider 未指定 (settings 既定) = 従来の均等ローテ + 残尺ベース選定。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from medialib.models import Asset, AssetKind, CmCreative, CmGrid, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent
from scheduling.models import AdBreak, Program, ProgramType
from scheduling.resolver import fill_break, resolve


def _cm(name, dur, aired=0):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title=name,
        duration_ms=dur,
        r2_key=f"mezzanine/cm/{name}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15, aired_count=aired)


def _program(channel, asset_ready, start, dur_ms=3_600_000, break_ms=0):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組",
        start_at=start,
        end_at=start + timedelta(milliseconds=dur_ms + break_ms),
        asset=asset_ready,
    )


def test_fill_break_remaining_based_long_first(channel, asset_ready):
    now = timezone.now()
    prog = _program(channel, asset_ready, now)
    br = AdBreak.objects.create(program=prog, offset_ms=600000, grid=CmGrid.G15, duration_ms=45000)
    _cm("cm30", 30000)
    _cm("cm15", 15000)
    pairs = fill_break(br, now)
    durs = [cm.asset.duration_ms for _it, cm in pairs]
    assert sum(durs) == 45000  # 残尺ぴったり充填
    assert durs[0] == 30000  # 長尺優先 (断片化防止)
    assert br.items.count() == len(pairs)  # AdBreakItem 永続化


def test_fill_break_returns_item_cm_pairs(channel, asset_ready):
    now = timezone.now()
    prog = _program(channel, asset_ready, now)
    br = AdBreak.objects.create(program=prog, offset_ms=600000, grid=CmGrid.G15, duration_ms=15000)
    cm = _cm("c", 15000)
    pairs = fill_break(br, now)
    assert len(pairs) == 1
    item, got = pairs[0]
    assert got.asset_id == cm.asset_id
    assert item.cm_asset_id == cm.asset_id  # AdBreakItem ⇔ CmCreative


def test_fill_break_reuses_existing_items(channel, asset_ready):
    from scheduling.models import AdBreakItem

    now = timezone.now()
    prog = _program(channel, asset_ready, now)
    br = AdBreak.objects.create(program=prog, offset_ms=600000, grid=CmGrid.G15, duration_ms=15000)
    cm = _cm("c", 15000)
    item = AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=cm)
    pairs = fill_break(br, now)
    assert [(it.id, c.asset_id) for it, c in pairs] == [(item.id, cm.asset_id)]
    assert br.items.count() == 1  # 再充填しない (決定性)


def test_resolve_sets_ad_break_item_on_play_cm(channel, asset_ready):
    now = timezone.now()
    start = now + timedelta(minutes=1)
    prog = _program(channel, asset_ready, start, break_ms=15000)
    AdBreak.objects.create(program=prog, offset_ms=1_800_000, grid=CmGrid.G15, duration_ms=15000)
    _cm("c", 15000)
    resolve(channel, now, now + timedelta(hours=2))
    pe = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_CM).first()
    assert pe is not None
    assert pe.ad_break_item_id is not None  # S10: 放確の逆引き経路
