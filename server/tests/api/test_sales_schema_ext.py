# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase A Commit1: 既存 app のスキーマ拡張 (series / ad_break_item FK / screening_status)。"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from medialib.models import (
    Asset,
    AssetKind,
    CmCreative,
    CmGrid,
    NormalizeStatus,
    ScreeningStatus,
)
from playout.models import PlayoutAction, PlayoutEvent
from scheduling.models import (
    AdBreak,
    AdBreakItem,
    Program,
    ProgramType,
    Series,
)


def _cm(advertiser="ADV"):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title="cm",
        duration_ms=15000,
        r2_key=f"mezzanine/cm/{advertiser}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(asset=a, advertiser=advertiser, grid=CmGrid.G15)


def test_series_and_program_link(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="レギュラーA")
    now = timezone.now()
    p = Program.objects.create(
        channel=channel,
        series=s,
        type=ProgramType.RECORDED,
        title="ep1",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    assert p.series_id == s.id
    assert list(s.programs.all()) == [p]


def test_program_series_optional(channel, asset_ready):
    now = timezone.now()
    p = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="単発",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    assert p.series_id is None  # 単発は NULL


def test_playout_event_ad_break_item_link(channel, asset_ready):
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
    item = AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=_cm())
    pe = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now,
        action=PlayoutAction.PLAY_CM,
        ad_break_item=item,
    )
    assert pe.ad_break_item_id == item.id


def test_cm_creative_screening_default_pending(db):
    cr = _cm("新規")
    assert cr.screening_status == ScreeningStatus.PENDING  # 新規は未考査
