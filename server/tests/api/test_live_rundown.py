# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""生キューシート (LiveRundown/LiveCue、タイムキープ Phase 1 / #25)。

読み取り (admin_live_rundown.LiveRundownOut) + 書き込み (scheduling.edit_views.live_cue_*) +
共有 idiom core.utils.move_ordered_item の単体テスト。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from core.models import LiveSource
from core.utils import move_ordered_item
from medialib.models import CmBundle
from scheduling.models import LiveCue, LiveCueKind, LiveCueState, LiveRundown, Program, ProgramType

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

_RUNDOWN_URL = "/api/v1/admin/scheduling/{slug}/live-rundown/{program_id}"


@pytest.fixture(autouse=True)
def _staff_login(client, staff_user):
    client.force_login(staff_user)


def _post(c, url, payload):
    return c.post(url, data=json.dumps(payload), content_type="application/json")


def _live_program(channel, start=BASE, end=None, title="生番組"):
    ls = LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="k")
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=end or start + timedelta(hours=1),
        live_source=ls,
    )


def _url(name, channel, **kw):
    from django.urls import reverse

    return reverse(f"scheduling:{name}", kwargs={"slug": channel.slug, **kw})


# ---- 読み取り: LiveRundownOut ----


def test_live_rundown_requires_auth(http_client, channel, db):
    prog = _live_program(channel)
    assert (
        http_client.get(_RUNDOWN_URL.format(slug=channel.slug, program_id=prog.id)).status_code
        == 401
    )


def test_live_rundown_empty(staff_client, channel, db):
    """rundown 未作成でも 200 (cues=[] / bundles・assets の選択肢だけ返す)。"""
    prog = _live_program(channel)
    d = staff_client.get(_RUNDOWN_URL.format(slug=channel.slug, program_id=prog.id)).json()
    assert d["program_id"] == prog.id
    assert d["program_title"] == "生番組"
    assert d["channel"]["slug"] == channel.slug
    assert d["cues"] == []
    assert d["planned_total_ms"] == 0
    assert d["over_under_ms"] == -3_600_000  # 60分枠 − 0


def test_live_rundown_populated(staff_client, channel, asset_ready, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = CmBundle.objects.create(name="束A")
    LiveCue.objects.create(
        rundown=rundown, seq=1, kind=LiveCueKind.SECTION, label="OP", planned_duration_ms=300_000
    )
    LiveCue.objects.create(
        rundown=rundown,
        seq=2,
        kind=LiveCueKind.CM,
        planned_duration_ms=30_000,
        cm_bundle=bundle,
        grid="15s",
    )
    LiveCue.objects.create(
        rundown=rundown, seq=3, kind=LiveCueKind.VT, planned_duration_ms=60_000, asset=asset_ready
    )

    d = staff_client.get(_RUNDOWN_URL.format(slug=channel.slug, program_id=prog.id)).json()
    assert [c["kind"] for c in d["cues"]] == ["section", "cm", "vt"]
    cm_row = d["cues"][1]
    assert cm_row["cm_bundle_id"] == bundle.id
    assert cm_row["cm_bundle_name"] == "束A"
    assert cm_row["grid"] == "15s"
    vt_row = d["cues"][2]
    assert vt_row["asset_id"] == asset_ready.id
    assert vt_row["asset_title"] == asset_ready.title
    assert d["planned_total_ms"] == 300_000 + 30_000 + 60_000
    assert d["over_under_ms"] == (300_000 + 30_000 + 60_000) - 3_600_000
    # 選択肢: bundle は名前一覧に含まれ、assets は usable_as_program=True かつ READY のみ
    assert any(b["id"] == bundle.id for b in d["bundles"])
    assert any(a["id"] == asset_ready.id for a in d["assets"])


def test_live_rundown_unknown_program_404(staff_client, channel, db):
    assert (
        staff_client.get(_RUNDOWN_URL.format(slug=channel.slug, program_id=999999)).status_code
        == 404
    )


# ---- 書き込み: live_cue_create ----


def test_live_cue_create_section_ok(client, channel, db):
    prog = _live_program(channel)
    res = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "section", "label": "本編", "planned_duration_ms": 600_000},
    )
    assert res.status_code == 200
    cue = LiveCue.objects.get(rundown__program=prog)
    assert cue.seq == 1
    assert cue.kind == "section"
    assert cue.label == "本編"
    assert cue.planned_duration_ms == 600_000
    assert cue.state == LiveCueState.PENDING
    assert cue.cm_bundle_id is None and cue.asset_id is None


def test_live_cue_create_appends_seq(client, channel, db):
    prog = _live_program(channel)
    _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "section", "planned_duration_ms": 60_000},
    )
    _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "section", "planned_duration_ms": 60_000},
    )
    seqs = sorted(LiveCue.objects.filter(rundown__program=prog).values_list("seq", flat=True))
    assert seqs == [1, 2]


def test_live_cue_create_cm_requires_grid_bundle_optional(client, channel, db):
    """CM cue は grid 必須・cm_bundle は任意 (無ければ発火時に grid 動的充填=D3 §11)。"""
    prog = _live_program(channel)
    bundle = CmBundle.objects.create(name="束A")
    # grid 不正 → 422
    res2 = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "cm", "planned_duration_ms": 30_000, "cm_bundle_id": bundle.id, "grid": "99s"},
    )
    assert res2.status_code == 422
    assert not LiveCue.objects.filter(rundown__program=prog).exists()
    # cm_bundle_id 無し + grid あり → 200 (動的充填 cue)
    dyn = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "cm", "planned_duration_ms": 30_000, "grid": "15s"},
    )
    assert dyn.status_code == 200
    dyn_cue = LiveCue.objects.get(pk=dyn.json()["id"])
    assert dyn_cue.cm_bundle_id is None and dyn_cue.grid == "15s"
    # bundle 割付あり → 200
    ok = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "cm", "planned_duration_ms": 30_000, "cm_bundle_id": bundle.id, "grid": "15s"},
    )
    assert ok.status_code == 200
    assert LiveCue.objects.get(pk=ok.json()["id"]).cm_bundle_id == bundle.id


def test_live_cue_create_vt_requires_asset(client, channel, asset_ready, db):
    prog = _live_program(channel)
    res = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "vt", "planned_duration_ms": 60_000},
    )
    assert res.status_code == 422
    assert not LiveCue.objects.filter(rundown__program=prog).exists()
    ok = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "vt", "planned_duration_ms": 60_000, "asset_id": asset_ready.id},
    )
    assert ok.status_code == 200
    cue = LiveCue.objects.get(rundown__program=prog)
    assert cue.asset_id == asset_ready.id


def test_live_cue_create_section_rejects_extra_fk(client, channel, asset_ready, db):
    prog = _live_program(channel)
    res = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "section", "planned_duration_ms": 60_000, "asset_id": asset_ready.id},
    )
    assert res.status_code == 422
    assert not LiveCue.objects.filter(rundown__program=prog).exists()


def test_live_cue_create_rejects_non_positive_duration(client, channel, db):
    prog = _live_program(channel)
    res = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "section", "planned_duration_ms": 0},
    )
    assert res.status_code == 400


def test_live_cue_create_rejects_recorded_program(client, channel, asset_ready, db):
    prog = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="録画",
        start_at=BASE,
        end_at=BASE + timedelta(hours=1),
        asset=asset_ready,
    )
    res = _post(
        client,
        _url("live_cue_create", channel, program_id=prog.id),
        {"kind": "section", "planned_duration_ms": 60_000},
    )
    assert res.status_code == 422
    assert not LiveRundown.objects.filter(program=prog).exists()


# ---- 書き込み: live_cue_update ----


def _cue(rundown, seq, kind=LiveCueKind.SECTION, **kw):
    return LiveCue.objects.create(
        rundown=rundown,
        seq=seq,
        kind=kind,
        planned_duration_ms=kw.pop("planned_duration_ms", 60_000),
        **kw,
    )


def test_live_cue_update_ok(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1, label="旧")
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {"label": "新", "planned_duration_ms": 90_000, "auto_fire": True, "auto_offset_ms": 5_000},
    )
    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.label == "新"
    assert cue.planned_duration_ms == 90_000
    assert cue.auto_fire is True
    assert cue.auto_offset_ms == 5_000


def test_live_cue_update_rejects_negative_auto_offset_ms(client, channel, db):
    """2026-07-04 レビュー指摘: auto_offset_ms の境界検証が無いと、resolver 側で
    timedelta(milliseconds=...) が OverflowError を起こしチャンネル丸ごとの resolve が
    止まりうる。境界(0〜24時間)は edit_views 側で先に弾く。"""
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1)
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {"planned_duration_ms": 60_000, "auto_fire": True, "auto_offset_ms": -1},
    )
    assert res.status_code == 422
    cue.refresh_from_db()
    assert cue.auto_offset_ms is None  # 保存されていないこと


def test_live_cue_update_rejects_absurdly_large_auto_offset_ms(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1)
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {"planned_duration_ms": 60_000, "auto_fire": True, "auto_offset_ms": 10**18},
    )
    assert res.status_code == 422
    cue.refresh_from_db()
    assert cue.auto_offset_ms is None


def test_live_cue_update_accepts_auto_offset_ms_at_upper_bound(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1)
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {
            "planned_duration_ms": 60_000,
            "auto_fire": True,
            "auto_offset_ms": 24 * 3600 * 1000,
        },
    )
    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.auto_offset_ms == 24 * 3600 * 1000


def test_live_cue_update_kind_immutable(client, channel, db):
    """POST body に kind を含めても無視され、既存 kind のまま (section の FK 検証で保存される)。"""
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1, kind=LiveCueKind.SECTION)
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {"kind": "cm", "planned_duration_ms": 60_000},
    )
    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.kind == LiveCueKind.SECTION  # 不変


def test_live_cue_update_not_pending_422(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1, state=LiveCueState.AIRED)
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {"label": "後から", "planned_duration_ms": 60_000},
    )
    assert res.status_code == 422
    cue.refresh_from_db()
    assert cue.label is None


def test_live_cue_update_cm_invalid_422(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = CmBundle.objects.create(name="束A")
    cue = _cue(rundown, 1, kind=LiveCueKind.CM, cm_bundle=bundle, grid="15s")
    res = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {"planned_duration_ms": 30_000},  # cm_bundle_id を落とす → 422
    )
    assert res.status_code == 422
    cue.refresh_from_db()
    assert cue.cm_bundle_id == bundle.id  # 変更されない


# ---- 書き込み: live_cue_move / live_cue_delete ----


def test_live_cue_move_swaps_seq(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    a = _cue(rundown, 1, label="A")
    b = _cue(rundown, 2, label="B")
    res = _post(client, _url("live_cue_move", channel, cue_id=b.id), {"direction": "up"})
    assert res.status_code == 200
    a.refresh_from_db()
    b.refresh_from_db()
    assert b.seq < a.seq


def test_live_cue_move_not_pending_422(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    a = _cue(rundown, 1, label="A", state=LiveCueState.AIRED)
    b = _cue(rundown, 2, label="B")
    res = _post(client, _url("live_cue_move", channel, cue_id=a.id), {"direction": "down"})
    assert res.status_code == 422
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.seq == 1 and b.seq == 2  # 並び順は変わらない


def test_live_cue_delete_ok(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1)
    res = client.post(_url("live_cue_delete", channel, cue_id=cue.id))
    assert res.status_code == 204
    assert not LiveCue.objects.filter(pk=cue.id).exists()


def test_live_cue_delete_not_pending_422(client, channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1, state=LiveCueState.SKIPPED)
    res = client.post(_url("live_cue_delete", channel, cue_id=cue.id))
    assert res.status_code == 422
    assert LiveCue.objects.filter(pk=cue.id).exists()


def test_live_cue_write_views_scoped_to_channel(client, channel, db):
    """別 ch の cue は 404 (channel スコープが正しく効くこと)。"""
    from core.models import Channel

    other = Channel.objects.create(name="別ch", slug="ch-other", enabled=True, agent_token="t2")
    prog = _live_program(other)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(rundown, 1)
    res = client.post(_url("live_cue_delete", channel, cue_id=cue.id))
    assert res.status_code == 404


# ---- core.utils.move_ordered_item 単体 ----


def test_move_ordered_item_up_and_down(channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    a = _cue(rundown, 1, label="A")
    b = _cue(rundown, 2, label="B")
    c = _cue(rundown, 3, label="C")

    move_ordered_item(b, rundown.cues, "up")
    a.refresh_from_db()
    b.refresh_from_db()
    c.refresh_from_db()
    assert b.seq < a.seq < c.seq  # B が先頭へ

    move_ordered_item(b, rundown.cues, "down")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.seq < b.seq  # 元に戻る


def test_move_ordered_item_noop_at_boundary(channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    a = _cue(rundown, 1, label="A")
    b = _cue(rundown, 2, label="B")
    move_ordered_item(a, rundown.cues, "up")  # 先頭なので隣接なし → no-op
    a.refresh_from_db()
    assert a.seq == 1
    move_ordered_item(b, rundown.cues, "down")  # 末尾なので隣接なし → no-op
    b.refresh_from_db()
    assert b.seq == 2


def test_move_ordered_item_invalid_direction_noop(channel, db):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    a = _cue(rundown, 1, label="A")
    move_ordered_item(a, rundown.cues, "sideways")
    a.refresh_from_db()
    assert a.seq == 1


# ---- 定番進行表テンプレ (LiveRundownTemplate/…Cue・Phase3 C / #25 §9) ----

from datetime import date  # noqa: E402

from scheduling.models import (  # noqa: E402
    LiveRundownTemplate,
    LiveRundownTemplateCue,
    Series,
    SeriesSlot,
)

_TMPL_URL = "/api/v1/admin/scheduling/{slug}/rundown-template/{slot_id}"


def _live_slot(channel, dur_ms=3_600_000):
    ls = LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="k")
    series = Series.objects.create(channel=channel, title="生レギュラー")
    return SeriesSlot.objects.create(
        series=series,
        dow=0,
        start_time="20:00",
        duration_ms=dur_ms,
        program_type=ProgramType.LIVE,
        live_source=ls,
        effective_from=date(2020, 1, 1),
    )


def _rec_slot(channel, asset):
    series = Series.objects.create(channel=channel, title="録画レギュラー")
    return SeriesSlot.objects.create(
        series=series,
        dow=0,
        start_time="21:00",
        duration_ms=1_800_000,
        program_type=ProgramType.RECORDED,
        default_asset=asset,
        effective_from=date(2020, 1, 1),
    )


def test_rundown_template_requires_auth(http_client, channel, db):
    slot = _live_slot(channel)
    assert http_client.get(_TMPL_URL.format(slug=channel.slug, slot_id=slot.id)).status_code == 401


def test_rundown_template_empty(staff_client, channel, db):
    slot = _live_slot(channel)
    d = staff_client.get(_TMPL_URL.format(slug=channel.slug, slot_id=slot.id)).json()
    assert d["slot_id"] == slot.id
    assert d["cues"] == []
    assert d["planned_total_ms"] == 0
    assert d["over_under_ms"] == -3_600_000  # 60分枠 − 0
    assert "生レギュラー" in d["slot_label"]


def test_rundown_template_populated(staff_client, channel, asset_ready, db):
    slot = _live_slot(channel)
    tmpl = LiveRundownTemplate.objects.create(slot=slot)
    bundle = CmBundle.objects.create(name="束A")
    LiveRundownTemplateCue.objects.create(
        template=tmpl, seq=1, kind=LiveCueKind.SECTION, label="OP", planned_duration_ms=300_000
    )
    LiveRundownTemplateCue.objects.create(
        template=tmpl,
        seq=2,
        kind=LiveCueKind.CM,
        planned_duration_ms=30_000,
        cm_bundle=bundle,
        grid="15s",
    )
    LiveRundownTemplateCue.objects.create(
        template=tmpl, seq=3, kind=LiveCueKind.VT, planned_duration_ms=60_000, asset=asset_ready
    )
    d = staff_client.get(_TMPL_URL.format(slug=channel.slug, slot_id=slot.id)).json()
    assert [c["kind"] for c in d["cues"]] == ["section", "cm", "vt"]
    # 雛形は state を持たないが行型互換のため "pending" を返す。
    assert all(c["state"] == "pending" and c["state_label"] == "" for c in d["cues"])
    assert d["cues"][1]["cm_bundle_id"] == bundle.id
    assert d["cues"][2]["asset_id"] == asset_ready.id
    assert d["planned_total_ms"] == 390_000
    assert d["over_under_ms"] == 390_000 - 3_600_000


def test_template_cue_create_ok(client, channel, db):
    slot = _live_slot(channel)
    r = _post(
        client,
        _url("template_cue_create", channel, slot_id=slot.id),
        {"kind": "section", "label": "OP", "planned_duration_ms": 300_000},
    )
    assert r.status_code == 200 and r.json()["ok"]
    tmpl = LiveRundownTemplate.objects.get(slot=slot)
    assert tmpl.cues.count() == 1 and tmpl.cues.first().seq == 1


def test_template_cue_create_cm_requires_grid(client, channel, db):
    slot = _live_slot(channel)
    # grid 不正 → 422
    bad = _post(
        client,
        _url("template_cue_create", channel, slot_id=slot.id),
        {"kind": "cm", "planned_duration_ms": 30_000, "grid": "99s"},
    )
    assert bad.status_code == 422
    # bundle 無し + grid あり → 200 (動的充填 cue・D3)
    ok = _post(
        client,
        _url("template_cue_create", channel, slot_id=slot.id),
        {"kind": "cm", "planned_duration_ms": 30_000, "grid": "15s"},
    )
    assert ok.status_code == 200
    assert LiveRundownTemplateCue.objects.get(pk=ok.json()["id"]).cm_bundle_id is None


def test_template_cue_create_rejects_recorded_slot(client, channel, asset_ready, db):
    slot = _rec_slot(channel, asset_ready)
    r = _post(
        client,
        _url("template_cue_create", channel, slot_id=slot.id),
        {"kind": "section", "planned_duration_ms": 300_000},
    )
    assert r.status_code == 422


def test_template_cue_update_and_auto_fire(client, channel, db):
    slot = _live_slot(channel)
    tmpl = LiveRundownTemplate.objects.create(slot=slot)
    bundle = CmBundle.objects.create(name="束A")
    cue = LiveRundownTemplateCue.objects.create(
        template=tmpl,
        seq=1,
        kind=LiveCueKind.CM,
        planned_duration_ms=30_000,
        cm_bundle=bundle,
        grid="15s",
    )
    r = _post(
        client,
        _url("template_cue_update", channel, cue_id=cue.id),
        {
            "planned_duration_ms": 45_000,
            "cm_bundle_id": bundle.id,
            "grid": "20s",
            "auto_fire": True,
            "auto_offset_ms": 600_000,
        },
    )
    assert r.status_code == 200
    cue.refresh_from_db()
    assert cue.planned_duration_ms == 45_000 and cue.grid == "20s"
    assert cue.auto_fire is True and cue.auto_offset_ms == 600_000


def test_template_cue_move_and_delete(client, channel, db):
    slot = _live_slot(channel)
    tmpl = LiveRundownTemplate.objects.create(slot=slot)
    a = LiveRundownTemplateCue.objects.create(
        template=tmpl, seq=1, kind=LiveCueKind.SECTION, planned_duration_ms=1000
    )
    b = LiveRundownTemplateCue.objects.create(
        template=tmpl, seq=2, kind=LiveCueKind.SECTION, planned_duration_ms=1000
    )
    r = _post(client, _url("template_cue_move", channel, cue_id=b.id), {"direction": "up"})
    assert r.status_code == 200
    a.refresh_from_db()
    b.refresh_from_db()
    assert b.seq < a.seq
    r = client.post(_url("template_cue_delete", channel, cue_id=a.id))
    assert r.status_code == 204
    assert not LiveRundownTemplateCue.objects.filter(pk=a.id).exists()


def test_live_cue_update_wallclock_requires_wall_time(client, channel, db):
    """auto_anchor=wallclock は auto_wall_time (HH:MM) 必須 (D1 §11)。"""
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    cue = _cue(
        rundown, 1, kind=LiveCueKind.CM, cm_bundle=CmBundle.objects.create(name="B"), grid="15s"
    )
    # wallclock だが時刻無し → 422
    bad = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {
            "planned_duration_ms": 30_000,
            "cm_bundle_id": cue.cm_bundle_id,
            "grid": "15s",
            "auto_fire": True,
            "auto_anchor": "wallclock",
        },
    )
    assert bad.status_code == 422
    # wallclock + 時刻あり → 200
    ok = _post(
        client,
        _url("live_cue_update", channel, cue_id=cue.id),
        {
            "planned_duration_ms": 30_000,
            "cm_bundle_id": cue.cm_bundle_id,
            "grid": "15s",
            "auto_fire": True,
            "auto_anchor": "wallclock",
            "auto_wall_time": "18:45",
        },
    )
    assert ok.status_code == 200
    cue.refresh_from_db()
    assert cue.auto_anchor == "wallclock" and cue.auto_wall_time.strftime("%H:%M") == "18:45"
