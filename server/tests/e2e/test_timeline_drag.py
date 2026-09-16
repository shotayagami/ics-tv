# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 グラフィカル編成タイムライン E2E (Playwright): 描画・ドラッグ移動・ブロック選択パネル。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from medialib.models import Asset, AssetKind, CmGrid, NormalizeStatus
from scheduling.models import AdBreak, Program, ProgramType

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def ch_with_program(db):
    from core.models import Channel

    channel = Channel.objects.create(name="ICS-TV TL", slug="tl1", enabled=True)
    asset = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="本編60分",
        duration_ms=3_600_000,
        r2_key="mezzanine/program/tl.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="昼の番組",
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        asset=asset,
    )
    AdBreak.objects.create(program=prog, offset_ms=1_800_000, grid=CmGrid.G15, duration_ms=120_000)
    return channel, prog


def test_timeline_renders_block(live_server, staff_page, ch_with_program):
    channel, _ = ch_with_program
    staff_page.goto(f"{live_server.url}/scheduling/ch/{channel.slug}/timeline/")
    staff_page.wait_for_selector(".tl-block")
    assert staff_page.locator(".tl-block").count() == 1
    assert "昼の番組" in staff_page.content()


def test_drag_move_changes_start(live_server, staff_page, ch_with_program):
    channel, prog = ch_with_program
    orig_start = prog.start_at
    staff_page.goto(f"{live_server.url}/scheduling/ch/{channel.slug}/timeline/")
    block = staff_page.wait_for_selector(".tl-block")
    box = block.bounding_box()
    # ブロック中央を掴んで下へ ~150px (後方へ移動) ドラッグ
    staff_page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 10)
    staff_page.mouse.down()
    staff_page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 160, steps=8)
    staff_page.mouse.up()
    # move POST → DB 反映を待つ
    for _ in range(20):
        prog.refresh_from_db()
        if prog.start_at > orig_start:
            break
        staff_page.wait_for_timeout(150)
    assert prog.start_at > orig_start  # 後方の時刻へ移動した
    assert prog.end_at - prog.start_at == timedelta(hours=1)  # 尺保持


def test_select_block_shows_panel(live_server, staff_page, ch_with_program):
    channel, _ = ch_with_program
    staff_page.goto(f"{live_server.url}/scheduling/ch/{channel.slug}/timeline/")
    staff_page.wait_for_selector(".tl-block")
    # Click the block → Alpine.js x-if="sel" panel appears with CM割当 link
    staff_page.locator(".tl-block").click()
    staff_page.wait_for_selector("a:has-text('CM割当')", timeout=5000)
    assert "昼の番組" in staff_page.content()
