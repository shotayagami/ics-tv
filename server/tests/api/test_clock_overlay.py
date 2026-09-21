# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""朝・夕の左上時計 (daypart corner clock): resolver が clock_windows を cg_cues(layer 36) に合成する。"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from medialib.models import Asset, AssetKind, FillerItem, FillerPlaylist, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent
from scheduling.models import Program, ProgramType, Series
from scheduling.resolver import CLOCK_LAYER, resolve

JST = ZoneInfo("Asia/Tokyo")


def _events(channel, action):
    return list(
        PlayoutEvent.objects.filter(channel=channel, action=action).order_by("scheduled_at")
    )


def _clock_cues(event) -> list[dict]:
    raw = event.params.get("cg_cues")
    if not raw:
        return []
    return [c for c in json.loads(raw) if c.get("layer") == CLOCK_LAYER]


def _enable_clock(channel, windows=None):
    channel.clock_overlay_enabled = True
    channel.clock_windows = windows if windows is not None else [{"start": "04:30", "end": "08:00"}]
    channel.save(update_fields=["clock_overlay_enabled", "clock_windows"])


def _make_program(channel, asset, **kw):
    now = timezone.now()
    start = kw.pop("start", now + timedelta(minutes=1))
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=kw.pop("title", "朝の番組"),
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
        **kw,
    )


# ---- Channel.effective_clock_windows ----


def test_effective_clock_windows_parses_and_drops_invalid(channel):
    channel.clock_windows = [{"start": "04:30", "end": "08:00"}, {"start": "bad"}, "junk"]
    assert channel.effective_clock_windows == [(time(4, 30), time(8, 0))]


def test_effective_clock_windows_falls_back_to_settings_default(channel):
    channel.clock_windows = []
    ws = channel.effective_clock_windows
    assert ws, "未設定なら settings.ICSTV_CLOCK_WINDOWS にフォールバックする"
    assert (time(4, 30), time(8, 0)) in ws


# ---- Program.resolved_clock_hidden (番組 → シリーズ 解決) ----


def test_resolved_clock_hidden_inherits_from_series(channel, asset_ready):
    s = Series.objects.create(channel=channel, title="映画劇場", clock_hidden=True)
    p = _make_program(channel, asset_ready, series=s)
    assert p.resolved_clock_hidden is True


def test_resolved_clock_hidden_program_level(channel, asset_ready):
    p = _make_program(channel, asset_ready, clock_hidden=True)
    assert p.resolved_clock_hidden is True


def test_resolved_clock_hidden_default_false(channel, asset_ready):
    p = _make_program(channel, asset_ready)
    assert p.resolved_clock_hidden is False


# ---- resolver: program へ clock cue を載せる ----


def test_program_gets_clock_cue_when_enabled(channel, asset_ready):
    _enable_clock(channel)
    now = timezone.now()
    _make_program(channel, asset_ready)
    resolve(channel, now, now + timedelta(hours=2))
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]
    cues = _clock_cues(head)
    assert cues, "有効時は本編 segment に clock cue が載る"
    c0 = cues[0]  # show_at 昇順 → 当日の最初の窓
    assert c0["template"] == "clock/corner"
    assert c0["kind"] == "graphic"
    sh = datetime.fromisoformat(c0["show_at"]).astimezone(JST)
    hi = datetime.fromisoformat(c0["hide_at"]).astimezone(JST)
    assert (sh.hour, sh.minute) == (4, 30)  # JST 04:30 開始
    assert (hi.hour, hi.minute) == (8, 0)  # JST 08:00 終了


def test_clock_cue_absent_when_disabled(channel, asset_ready):
    # channel.clock_overlay_enabled は既定 False
    now = timezone.now()
    _make_program(channel, asset_ready)
    resolve(channel, now, now + timedelta(hours=2))
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]
    assert _clock_cues(head) == []


def test_clock_cue_absent_when_program_hidden(channel, asset_ready):
    _enable_clock(channel)
    now = timezone.now()
    _make_program(channel, asset_ready, clock_hidden=True)
    resolve(channel, now, now + timedelta(hours=2))
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]
    assert _clock_cues(head) == [], "clock_hidden の番組には時計を載せない"


def test_clock_cue_default_windows_when_channel_windows_empty(channel, asset_ready):
    channel.clock_overlay_enabled = True
    channel.clock_windows = []  # → settings 既定 (朝/夕 2 窓)
    channel.save(update_fields=["clock_overlay_enabled", "clock_windows"])
    now = timezone.now()
    _make_program(channel, asset_ready)
    resolve(channel, now, now + timedelta(hours=2))
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]
    # 2 窓 × (当日 + 翌日) = 4 cue。少なくとも複数窓が載ること。
    assert len(_clock_cues(head)) >= 2


# ---- resolver: filler は常に clock cue を載せる ----


def test_filler_gets_clock_cue_when_enabled(channel, asset_ready):
    _enable_clock(channel)
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
    now = timezone.now()
    # 30 分先に番組 → 手前にフィラーギャップ
    _make_program(channel, asset_ready, start=now + timedelta(minutes=30))
    resolve(channel, now, now + timedelta(hours=2))
    fillers = _events(channel, PlayoutAction.PLAY_FILLER)
    assert fillers
    assert _clock_cues(fillers[0]), "フィラーは時間帯内で常に時計対象"


def test_clock_cues_sorted_by_show_at(channel, asset_ready):
    """同一レイヤの複数 cue は show_at 昇順 (agent の過去窓 CLEAR が現窓 show より先に来る前提)。"""
    _enable_clock(
        channel, windows=[{"start": "15:00", "end": "19:00"}, {"start": "04:30", "end": "08:00"}]
    )
    now = timezone.now()
    _make_program(channel, asset_ready)
    resolve(channel, now, now + timedelta(hours=2))
    head = _events(channel, PlayoutAction.PLAY_ASSET)[0]
    shows = [c["show_at"] for c in _clock_cues(head)]
    assert shows == sorted(shows)
