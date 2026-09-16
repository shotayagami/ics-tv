# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CG イベント配線 (casparcg.md §3.4): resolve が Lバー/CM バンパーの派生ヒントを params に載せる。"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from medialib.models import (
    Asset,
    AssetKind,
    CmCreative,
    CmGrid,
    FillerItem,
    FillerPlaylist,
    NormalizeStatus,
)
from playout.models import PlayoutAction, PlayoutEvent
from scheduling.models import AdBreak, Program, ProgramType
from scheduling.resolver import resolve


def _cm(name="c"):
    a = Asset.objects.create(
        kind=AssetKind.CM, title=name, duration_ms=15000, normalize_status=NormalizeStatus.READY
    )
    return CmCreative.objects.create(asset=a, advertiser=name, grid=CmGrid.G15)


def _resolve_with_break(channel, asset_ready):
    now = timezone.now()
    start = now + timedelta(minutes=1)
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="夜のニュース",
        start_at=start,
        end_at=start + timedelta(milliseconds=3_600_000 + 15000),
        asset=asset_ready,
    )
    AdBreak.objects.create(program=prog, offset_ms=1_800_000, grid=CmGrid.G15, duration_ms=15000)
    _cm("cm1")
    resolve(channel, now, now + timedelta(hours=2))
    return prog


def _events(channel, action):
    return list(
        PlayoutEvent.objects.filter(channel=channel, action=action).order_by("scheduled_at")
    )


def test_head_asset_has_lbar_hint(channel, asset_ready):
    _resolve_with_break(channel, asset_ready)
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]
    assert head.params.get("cg_lbar_add") == "1"
    assert head.params.get("cg_title") == "夜のニュース"
    assert head.params.get("cg_channel") == channel.name


def test_first_cm_has_cm_in_hint(channel, asset_ready):
    _resolve_with_break(channel, asset_ready)
    cm = _events(channel, PlayoutAction.PLAY_CM)[0]
    assert cm.params.get("cg_cm_in") == "1"


def test_post_break_asset_has_cm_out_hint(channel, asset_ready):
    _resolve_with_break(channel, asset_ready)
    assets = _events(channel, PlayoutAction.PLAY_ASSET)
    # 番組頭以外の本編 (CM 明け) は cg_cm_out
    assert assets[0].params.get("cg_cm_out") is None  # 頭は lbar_add 側
    assert any(a.params.get("cg_cm_out") == "1" for a in assets[1:])


def test_cm_out_asset_keeps_title(channel, asset_ready):
    """CM 明けの本編 segment も cg_title を保持する (Lバーのタイトルが空に化けない)。"""
    _resolve_with_break(channel, asset_ready)
    assets = _events(channel, PlayoutAction.PLAY_ASSET)
    cm_out = [a for a in assets if a.params.get("cg_cm_out") == "1"]
    assert cm_out and all(a.params.get("cg_title") == "夜のニュース" for a in cm_out)


def test_program_has_preview_when_next_exists(channel, asset_ready):
    """番組枠は次番組があれば cg_preview_at (残り3分) + 次番組情報を全本編 segment に持つ。"""
    now = timezone.now()
    s1 = now + timedelta(minutes=1)
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組1",
        start_at=s1,
        end_at=s1 + timedelta(hours=1),
        asset=asset_ready,
    )
    s2 = s1 + timedelta(hours=1)  # 背中合わせ
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組2",
        start_at=s2,
        end_at=s2 + timedelta(hours=1),
        asset=asset_ready,
    )
    resolve(channel, now, now + timedelta(hours=3))
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]  # 番組1 の頭
    assert head.params.get("cg_title") == "番組1"
    assert head.params.get("cg_next_title") == "番組2"
    assert "cg_preview_at" in head.params  # 残り3分タイマー時刻 (ISO)
    assert "cg_preview_show" not in head.params  # 番組枠は常時表示ではない


def test_filler_has_blank_lbar_and_preview(channel, asset_ready):
    """フィラーは Lバー(タイトル空) + 次番組予告(常時) を持つ。"""
    now = timezone.now()
    fa = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="filler",
        duration_ms=300000,
        r2_key="mezzanine/filler/1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    pl = FillerPlaylist.objects.create(name="pl")
    FillerItem.objects.create(filler_playlist=pl, seq=1, asset=fa)
    channel.default_filler = pl
    channel.save(update_fields=["default_filler"])
    start = now + timedelta(minutes=30)
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="次番組",
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset_ready,
    )
    resolve(channel, now, now + timedelta(hours=2))
    fillers = _events(channel, PlayoutAction.PLAY_FILLER)
    assert fillers
    f = fillers[0]
    assert f.params.get("cg_title") == ""  # フィラーはロゴ+時計のみ (タイトル空)
    assert f.params.get("cg_channel") == channel.name
    assert f.params.get("cg_preview_show") == "1"  # フィラー中は常時予告
    assert f.params.get("cg_next_title") == "次番組"
    assert "cg_next_start" in f.params


def test_no_breaks_only_head_lbar(channel, asset_ready):
    now = timezone.now()
    start = now + timedelta(minutes=1)
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="単発番組",
        start_at=start,
        end_at=start + timedelta(milliseconds=3_600_000),
        asset=asset_ready,
    )
    resolve(channel, now, now + timedelta(hours=2))
    assets = _events(channel, PlayoutAction.PLAY_ASSET)
    assert len(assets) == 1  # 枠なし = 単一セグメント
    assert assets[0].params.get("cg_lbar_add") == "1"
    assert assets[0].params.get("cg_cm_out") is None  # CM 明けは無い
