# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-9): 営業 CM割付 sales allocation 概観 admin API。

staff_auth ゲート + 日付/チャンネル単位の 番組→CM枠→配置CM 構造。自動補充/入替の操作は既存
sales エンドポイント (tests/api/test_*) の責務。ここは GET 集約の構造を検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from scheduling.models import Program, ProgramType

_URL = "/api/v1/admin/sales/allocation"


def test_alloc_requires_auth(http_client, db):
    assert http_client.get(_URL).status_code == 401


def test_alloc_staff_structure(staff_client, channel, asset_ready, db):
    now = timezone.now()
    Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="割付番組",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    d = staff_client.get(f"{_URL}?channel={channel.id}").json()
    assert d["channel_id"] == channel.id and d["channel_name"] == channel.name
    assert any(c["id"] == channel.id for c in d["channels"])
    assert d["edit_url"] == "/sales/allocation/"
    assert d["day"] == timezone.localdate().isoformat()
    assert "割付番組" in [r["title"] for r in d["rows"]]  # 当日範囲内


def test_alloc_date_filter(staff_client, channel, db):
    d = staff_client.get(f"{_URL}?channel={channel.id}&date=2020-01-01").json()
    assert d["day"] == "2020-01-01" and d["rows"] == []  # 過去日は番組なし


def test_alloc_defaults_first_channel(staff_client, channel, db):
    d = staff_client.get(_URL).json()
    assert d["channel_id"] == channel.id  # channel 省略 → 先頭


def test_alloc_item_ids_and_cm_options(staff_client, channel, asset_ready, db):
    """2e-3: 各枠 item に item_id・break に grid・候補 cm_options(考査OKのみ) が出る。"""
    from medialib.models import (
        Asset,
        AssetKind,
        CmCreative,
        CmGrid,
        NormalizeStatus,
        ScreeningStatus,
    )
    from scheduling.models import AdBreak, AdBreakItem

    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="CM枠番組",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    br = AdBreak.objects.create(
        program=prog, offset_ms=600_000, grid=CmGrid.G15, duration_ms=15_000
    )
    cm_asset = Asset.objects.create(
        kind=AssetKind.CM, title="CM A", duration_ms=15_000, normalize_status=NormalizeStatus.READY
    )
    cm = CmCreative.objects.create(
        asset=cm_asset,
        advertiser="考査OK社",
        grid=CmGrid.G15,
        screening_status=ScreeningStatus.APPROVED,
    )
    item = AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=cm)
    # 考査PENDING は候補に出ない
    pend_asset = Asset.objects.create(
        kind=AssetKind.CM, title="CM P", duration_ms=15_000, normalize_status=NormalizeStatus.READY
    )
    CmCreative.objects.create(
        asset=pend_asset,
        advertiser="未考査社",
        grid=CmGrid.G15,
        screening_status=ScreeningStatus.PENDING,
    )

    d = staff_client.get(f"{_URL}?channel={channel.id}").json()
    row = next(r for r in d["rows"] if r["title"] == "CM枠番組")
    b = row["breaks"][0]
    assert b["grid"] == CmGrid.G15.value
    assert b["items"][0]["item_id"] == item.id
    # 2e-add: 枠 id・残尺・満杯フラグ (任意 CM 新規追加の判定に使う)。15s×1 枠を 1 本使用 = 満杯。
    assert b["break_id"] == br.id
    assert b["remaining"] == "0:00" and b["full"] is True
    advertisers = [o["name"] for o in d["cm_options"]]
    assert "考査OK社" in advertisers and "未考査社" not in advertisers  # 考査OKのみ
    assert all(o["grid"] for o in d["cm_options"])


def test_alloc_break_has_room_when_not_full(staff_client, channel, asset_ready, db):
    """2e-add: 枠尺 > 使用尺なら full=False・remaining に残りが出る (任意 CM 追加可)。"""
    from medialib.models import (
        Asset,
        AssetKind,
        CmCreative,
        CmGrid,
        NormalizeStatus,
        ScreeningStatus,
    )
    from scheduling.models import AdBreak, AdBreakItem

    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="余枠番組",
        start_at=now,
        end_at=now + timedelta(hours=1),
        asset=asset_ready,
    )
    # 45s の 15s 枠に 15s CM を 1 本 = 残 30s (あと 2 本)。
    br = AdBreak.objects.create(
        program=prog, offset_ms=600_000, grid=CmGrid.G15, duration_ms=45_000
    )
    cm_asset = Asset.objects.create(
        kind=AssetKind.CM, title="CM A", duration_ms=15_000, normalize_status=NormalizeStatus.READY
    )
    cm = CmCreative.objects.create(
        asset=cm_asset, advertiser="社A", grid=CmGrid.G15, screening_status=ScreeningStatus.APPROVED
    )
    AdBreakItem.objects.create(ad_break=br, seq=0, cm_asset=cm)

    d = staff_client.get(f"{_URL}?channel={channel.id}").json()
    b = next(r for r in d["rows"] if r["title"] == "余枠番組")["breaks"][0]
    assert b["full"] is False and b["remaining"] == "0:30"
