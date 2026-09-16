# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""自動グラフィックセット (GraphicCue) 専用編集 UI + 展開コピー (#18 §C)。"""

from __future__ import annotations

from datetime import date

import pytest
from django.urls import reverse

from scheduling.models import GraphicCue, GraphicKind, ProgramType, Series, SeriesSlot
from scheduling.tasks import expand_series_slots


@pytest.fixture(autouse=True)
def _staff_login(client, staff_user):
    client.force_login(staff_user)  # staff_member_required


def _series(channel):
    return Series.objects.create(channel=channel, title="提供番組")


def _u(name, channel, **kw):
    return reverse(f"scheduling:{name}", kwargs={"slug": channel.slug, **kw})


def test_filler_page_renders(client, channel, db):
    res = client.get(_u("graphic_cues", channel, owner="filler", owner_id=channel.id))
    assert res.status_code == 200
    assert "フィラー" in res.content.decode("utf-8")


def test_add_graphic_cue_with_text_and_position(client, channel, db):
    s = _series(channel)
    res = client.post(
        _u("graphic_cue_add", channel, owner="series", owner_id=s.id),
        {
            "layer": "45",
            "kind": "graphic",
            "text": "提供",
            "x": "5",
            "y": "80",
            "w": "25",
            "size": "34",
            "align": "left",
            "show_at_s": "10",
            "hide_at_s": "70",
        },
    )
    assert res.status_code == 302
    c = GraphicCue.objects.get(series=s)
    assert c.layer == 45 and c.kind == GraphicKind.GRAPHIC
    assert c.data == {
        "elements": [{"text": "提供", "x": 5, "y": 80, "w": 25, "size": 34, "align": "left"}]
    }
    assert c.show_at_ms == 10_000 and c.hide_at_ms == 70_000


def test_add_text_cue_open_ended(client, channel, db):
    s = _series(channel)
    client.post(
        _u("graphic_cue_add", channel, owner="series", owner_id=s.id),
        {"layer": "40", "kind": "text", "text": "速報", "show_at_s": "0"},
    )
    c = GraphicCue.objects.get(series=s)
    assert c.kind == GraphicKind.TEXT and c.data == {"text": "速報"}
    assert c.show_at_ms == 0 and c.hide_at_ms is None  # 空 = 末尾まで


def test_layer_90_rejected(client, channel, db):
    s = _series(channel)
    res = client.post(
        _u("graphic_cue_add", channel, owner="series", owner_id=s.id),
        {"layer": "90", "kind": "text", "text": "x"},
    )
    assert res.status_code == 302 and "err=" in res["Location"]
    assert not GraphicCue.objects.filter(series=s).exists()


def test_delete_cue(client, channel, db):
    s = _series(channel)
    c = GraphicCue.objects.create(series=s, layer=45, kind="text", data={"text": "x"})
    res = client.post(
        reverse(
            "scheduling:graphic_cue_delete",
            kwargs={"slug": channel.slug, "owner": "series", "owner_id": s.id, "cue_id": c.id},
        )
    )
    assert res.status_code == 302
    assert not GraphicCue.objects.filter(pk=c.id).exists()


def test_series_cues_copied_to_program_on_expand(channel, asset_ready, db):
    s = _series(channel)
    GraphicCue.objects.create(
        series=s,
        layer=45,
        kind="graphic",
        data={"elements": [{"text": "提供"}]},
        show_at_ms=5_000,
        hide_at_ms=65_000,
    )
    SeriesSlot.objects.create(
        series=s,
        dow=0,
        start_time="19:00",
        duration_ms=1_800_000,
        program_type=ProgramType.RECORDED,
        default_asset=asset_ready,
        effective_from=date(2020, 1, 1),
    )
    res = expand_series_slots(weeks=2)
    assert res["created"] >= 1
    # 各回 Program へ原本がコピーされている (個別編集可能)
    prog_cues = GraphicCue.objects.filter(program__series=s)
    assert prog_cues.count() == res["created"]
    pc = prog_cues.first()
    assert pc.layer == 45 and pc.data == {"elements": [{"text": "提供"}]}
    assert pc.show_at_ms == 5_000 and pc.hide_at_ms == 65_000
