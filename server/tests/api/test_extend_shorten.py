# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""押え (extend) / 巻き (shorten) サービスのテスト (#7 O-C / docs/operations.md O1・O8)。

extend は「後続を 48h 窓内の隙間で完全吸収できる場合のみ繰り下げ、不能ならカスケード暴走
防止で拒否」。shorten は live 限定で end_at を縮めるのみ。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from scheduling.models import Program, ProgramType
from scheduling.services import (
    ExtendRejectedError,
    ShortenRejectedError,
    extend_program,
    shorten_program,
)

MIN = 60_000  # 1 分 (ms)


def _ls():
    from core.models import LiveSource

    return LiveSource.objects.create(name="src", rtmp_app="live", rtmp_key="ch1")


def _rec(channel, asset, start, end, title="P"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=end,
        asset=asset,
    )


def _live(channel, ls, start, end, title="L"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=end,
        live_source=ls,
    )


# ---- 押え (extend) ----


def test_extend_no_following_just_extends(channel, asset_ready):
    base = timezone.now() + timedelta(hours=1)
    a = _rec(channel, asset_ready, base, base + timedelta(hours=1))
    extend_program(a.id, 10 * MIN)
    a.refresh_from_db()
    assert a.end_at == base + timedelta(hours=1, minutes=10)


def test_extend_absorbed_by_gap_no_shift(channel, asset_ready):
    """後続まで 30 分の隙間 → 10 分延長は隙間で完全吸収、後続は動かない。"""
    base = timezone.now() + timedelta(hours=1)
    a = _rec(channel, asset_ready, base, base + timedelta(hours=1), "A")
    b = _rec(
        channel,
        asset_ready,
        base + timedelta(hours=1, minutes=30),
        base + timedelta(hours=2, minutes=30),
        "B",
    )
    b_start = b.start_at
    extend_program(a.id, 10 * MIN)
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.end_at == base + timedelta(hours=1, minutes=10)
    assert b.start_at == b_start  # 隙間で吸収 → 不変


def test_extend_shifts_following_when_downstream_gap_absorbs(channel, asset_ready):
    """A→B 隣接、B→C に 15 分の隙間。A を 10 分延長 → B が 10 分繰り下がり C で吸収。"""
    base = timezone.now() + timedelta(hours=1)
    a = _rec(channel, asset_ready, base, base + timedelta(hours=1), "A")
    b = _rec(
        channel,
        asset_ready,
        base + timedelta(hours=1),
        base + timedelta(hours=2),
        "B",
    )
    c = _rec(
        channel,
        asset_ready,
        base + timedelta(hours=2, minutes=15),
        base + timedelta(hours=3),
        "C",
    )
    c_start = c.start_at
    extend_program(a.id, 10 * MIN)
    a.refresh_from_db()
    b.refresh_from_db()
    c.refresh_from_db()
    assert a.end_at == base + timedelta(hours=1, minutes=10)
    assert b.start_at == base + timedelta(hours=1, minutes=10)  # 10 分繰り下がり
    assert b.end_at == base + timedelta(hours=2, minutes=10)
    assert c.start_at == c_start  # 15 分隙間が 10 分を吸収 → C 不変


def test_extend_rejected_when_cannot_absorb(channel, asset_ready):
    """A→B 隣接で下流に隙間なし (B が最後) → 吸収不能で拒否 (カスケード暴走防止)。"""
    base = timezone.now() + timedelta(hours=1)
    a = _rec(channel, asset_ready, base, base + timedelta(hours=1), "A")
    _rec(channel, asset_ready, base + timedelta(hours=1), base + timedelta(hours=2), "B")
    with pytest.raises(ExtendRejectedError):
        extend_program(a.id, 10 * MIN)


def test_extend_rejected_near_end(channel, asset_ready):
    """終了 60 秒前を切った番組は延長不可。"""
    now = timezone.now()
    a = _rec(channel, asset_ready, now - timedelta(hours=1), now + timedelta(seconds=30))
    with pytest.raises(ExtendRejectedError):
        extend_program(a.id, 10 * MIN)


# ---- 巻き (shorten) ----


def test_shorten_live_reduces_end(channel):
    base = timezone.now() + timedelta(hours=1)
    ls = _ls()
    p = _live(channel, ls, base, base + timedelta(hours=1))
    shorten_program(p.id, 10 * MIN)
    p.refresh_from_db()
    assert p.end_at == base + timedelta(minutes=50)


def test_shorten_recorded_rejected(channel, asset_ready):
    base = timezone.now() + timedelta(hours=1)
    p = _rec(channel, asset_ready, base, base + timedelta(hours=1))
    with pytest.raises(ShortenRejectedError):
        shorten_program(p.id, 10 * MIN)


def test_shorten_below_floor_rejected(channel):
    base = timezone.now() + timedelta(hours=1)
    ls = _ls()
    p = _live(channel, ls, base, base + timedelta(minutes=10))
    with pytest.raises(ShortenRejectedError):
        shorten_program(p.id, 20 * MIN)  # new_end < start_at
