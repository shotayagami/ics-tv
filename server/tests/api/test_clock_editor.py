# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""時計エディタ API: GET /api/v1/admin/channels/{slug}/clock / POST admin-ui/ch/{slug}/clock-style/"""

from __future__ import annotations

import json

import pytest
from django.test import Client


@pytest.fixture()
def staff_client(db, django_user_model):
    u = django_user_model.objects.create_user(username="staff_ce", password="pw", is_staff=True)
    c = Client()
    c.force_login(u)
    return c


@pytest.fixture()
def channel(db):
    from core.models import Channel

    return Channel.objects.create(name="テスト局", slug="test-clock-ch")


def test_clock_get_defaults(staff_client, channel):
    res = staff_client.get(f"/api/v1/admin/channels/{channel.slug}/clock")
    assert res.status_code == 200
    data = res.json()
    assert data["font_family"] == "noto-sans-jp"
    assert data["time_size"] == 54
    assert data["position"] == "top-left"
    assert data["clock_overlay_enabled"] is False
    assert data["clock_windows"] == []
    assert data["save_url"].endswith(f"/admin-ui/ch/{channel.slug}/clock-style/")


def test_clock_post_save_and_reload(staff_client, channel):
    payload = {
        "font_family": "orbitron",
        "time_size": "60",
        "font_weight": "700",
        "time_color": "#ff0000",
        "date_color": "#00ff00",
        "text_effect": "glow",
        "bg_preset": "frosted",
        "bg_opacity": "0.5",
        "box_shadow": "lg",
        "border_radius": "pill",
        "entrance_anim": "slide-down",
        "show_seconds": "true",
        "show_date": "true",
        "position": "bottom-right",
        "clock_overlay_enabled": "true",
        "clock_windows": "05:00-09:00",  # HH:MM-HH:MM per-line format
    }
    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/clock-style/", payload)
    assert res.status_code == 200

    # GET で反映を確認
    res2 = staff_client.get(f"/api/v1/admin/channels/{channel.slug}/clock")
    assert res2.status_code == 200
    d = res2.json()
    assert d["font_family"] == "orbitron"
    assert d["time_size"] == 60
    assert d["time_color"] == "#ff0000"
    assert d["position"] == "bottom-right"
    assert d["show_seconds"] is True
    assert d["clock_overlay_enabled"] is True
    assert len(d["clock_windows"]) == 1
    assert d["clock_windows"][0]["start"] == "05:00"


def test_clock_post_invalid_font(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/clock-style/",
        {
            "font_family": "comic-sans",
            "time_size": "54",
            "font_weight": "700",
            "time_color": "#ffffff",
            "date_color": "#cfe3ff",
            "text_effect": "none",
            "bg_preset": "none",
            "bg_opacity": "0.8",
            "box_shadow": "md",
            "border_radius": "md",
            "entrance_anim": "none",
            "position": "top-left",
            "clock_overlay_enabled": "false",
            "clock_windows": "[]",
        },
    )
    assert res.status_code == 400


def test_clock_post_invalid_color(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/clock-style/",
        {
            "font_family": "noto-sans-jp",
            "time_size": "54",
            "font_weight": "700",
            "time_color": "red",  # hex ではない
            "date_color": "#cfe3ff",
            "text_effect": "none",
            "bg_preset": "none",
            "bg_opacity": "0.8",
            "box_shadow": "md",
            "border_radius": "md",
            "entrance_anim": "none",
            "position": "top-left",
            "clock_overlay_enabled": "false",
            "clock_windows": "[]",
        },
    )
    assert res.status_code == 400


def test_clock_post_size_out_of_range(staff_client, channel):
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/clock-style/",
        {
            "font_family": "noto-sans-jp",
            "time_size": "120",  # 範囲外 (36-80)
            "font_weight": "700",
            "time_color": "#ffffff",
            "date_color": "#cfe3ff",
            "text_effect": "none",
            "bg_preset": "none",
            "bg_opacity": "0.8",
            "box_shadow": "md",
            "border_radius": "md",
            "entrance_anim": "none",
            "position": "top-left",
            "clock_overlay_enabled": "false",
            "clock_windows": "[]",
        },
    )
    assert res.status_code == 400


def test_clock_style_override_series(staff_client, channel):
    """シリーズへの clock_style_override 保存 → series-form 読み戻しで反映。"""
    from scheduling.models import Series

    series = Series.objects.create(channel=channel, title="テスト番組")

    override = {"font_family": "orbitron", "position": "bottom-left"}
    res = staff_client.post(
        f"/api/v1/admin/scheduling/{channel.slug}/series/{series.id}",
        data=json.dumps(
            {
                "title": "テスト番組",
                "clock_style_override": override,
            }
        ),
        content_type="application/json",
    )
    assert res.status_code == 200

    res2 = staff_client.get(
        f"/api/v1/admin/scheduling/{channel.slug}/series-form",
        {"series_id": series.id},
    )
    assert res2.status_code == 200
    initial = res2.json()["initial"]
    assert initial["clock_style_override"]["font_family"] == "orbitron"


def test_clock_style_override_program(staff_client, channel):
    """番組への clock_style_override 保存 → program-form 読み戻しで反映。"""
    from datetime import timedelta

    from django.utils import timezone

    from medialib.models import Asset, AssetKind, NormalizeStatus
    from scheduling.models import Program, ProgramType, Series

    asset = Asset.objects.create(
        title="素材",
        kind=AssetKind.PROGRAM,
        duration_ms=60_000,
        normalize_status=NormalizeStatus.READY,
    )
    series = Series.objects.create(channel=channel, title="S")
    now = timezone.now()
    end = now + timedelta(minutes=1)
    prog = Program.objects.create(
        channel=channel,
        series=series,
        title="EP1",
        type=ProgramType.RECORDED,
        asset=asset,
        start_at=now,
        end_at=end,
    )

    override = {"bg_preset": "frosted", "entrance_anim": "zoom"}
    res = staff_client.post(
        f"/api/v1/admin/scheduling/{channel.slug}/programs/{prog.id}",
        data=json.dumps(
            {
                "title": "EP1",
                "type": "recorded",
                "start_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "end_at": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "asset_id": asset.id,
                "clock_style_override": override,
            }
        ),
        content_type="application/json",
    )
    assert res.status_code == 200

    res2 = staff_client.get(
        f"/api/v1/admin/scheduling/{channel.slug}/program-form",
        {"program_id": prog.id},
    )
    assert res2.status_code == 200
    initial = res2.json()["initial"]
    assert initial["clock_style_override"]["bg_preset"] == "frosted"


def test_resolved_clock_style_program_over_series(db, channel):
    """番組 clock_style_override がシリーズのそれより優先される。"""
    from datetime import timedelta

    from django.utils import timezone

    from medialib.models import Asset, AssetKind, NormalizeStatus
    from scheduling.models import Program, ProgramType, Series

    asset = Asset.objects.create(
        title="素材",
        kind=AssetKind.PROGRAM,
        duration_ms=60_000,
        normalize_status=NormalizeStatus.READY,
    )
    series = Series.objects.create(
        channel=channel,
        title="S",
        clock_style_override={"position": "top-right"},
    )
    now = timezone.now()
    prog = Program.objects.create(
        channel=channel,
        series=series,
        title="EP",
        type=ProgramType.RECORDED,
        asset=asset,
        start_at=now,
        end_at=now + timedelta(minutes=1),
    )
    # 番組に上書き設定なし → シリーズのが使われる
    assert prog.resolved_clock_style_override == {"position": "top-right"}

    # 番組に明示的な上書き → それが最優先
    prog.clock_style_override = {"position": "bottom-left"}
    prog.save(update_fields=["clock_style_override"])
    prog.refresh_from_db()
    assert prog.resolved_clock_style_override == {"position": "bottom-left"}
