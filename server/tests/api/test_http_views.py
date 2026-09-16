# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP view の smoke テスト (Django test Client)。"""

from __future__ import annotations


def test_admin_root_redirects_to_studio_spa(staff_client, db):
    """管理ホストのトップは新 studio SPA (/studio/) へ 302 リダイレクトする。"""
    res = staff_client.get("/")
    assert res.status_code == 302
    assert res.headers["Location"] == "/studio/"


def test_timeline_renders_for_known_channel(staff_client, channel):
    res = staff_client.get(f"/scheduling/ch/{channel.slug}/timeline/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "編成タイムライン" in body
    assert channel.name in body


def test_timeline_404_for_unknown_channel(staff_client, db):
    res = staff_client.get("/scheduling/ch/nonexistent/timeline/")
    assert res.status_code == 404


def test_program_create_get_renders_form(staff_client, channel):
    res = staff_client.get(f"/scheduling/ch/{channel.slug}/programs/new/")
    assert res.status_code == 200
    assert "番組追加" in res.content.decode("utf-8")


def test_program_create_autocalcs_end_from_asset_duration(staff_client, channel, asset_ready):
    """終了時刻を空欄で送ると素材尺 (1h) から自動算出される。"""
    from datetime import timedelta

    from django.utils import timezone

    from scheduling.models import Program

    start = (timezone.now() + timedelta(hours=1)).replace(microsecond=0)
    res = staff_client.post(
        f"/scheduling/ch/{channel.slug}/programs/new/",
        data={
            "title": "自動算出A",
            "type": "recorded",
            "start_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "asset": asset_ready.id,  # duration_ms=3_600_000 (1h)
            "public_visible": "on",
            "vod_visibility": "off",
        },
    )
    assert res.status_code == 302
    prog = Program.objects.get(title="自動算出A")
    assert (prog.end_at - prog.start_at) == timedelta(milliseconds=asset_ready.duration_ms)


def test_program_create_autocalcs_end_from_cuesheet_total(staff_client, channel, asset_ready):
    """キューシートがあれば素材尺 + Σ(CM枠尺) の積算尺で終了時刻を自動算出。"""
    from datetime import timedelta

    from django.utils import timezone

    from medialib.models import CmGrid, CueKind, CuePoint, CueSheet
    from scheduling.models import Program

    cue = CueSheet.objects.create(asset=asset_ready)
    CuePoint.objects.create(
        cue_sheet=cue, seq=1, kind=CueKind.CONTENT, duration_ms=asset_ready.duration_ms
    )
    CuePoint.objects.create(
        cue_sheet=cue, seq=2, kind=CueKind.AD_BREAK, duration_ms=15_000, grid=CmGrid.G15
    )

    start = (timezone.now() + timedelta(hours=1)).replace(microsecond=0)
    res = staff_client.post(
        f"/scheduling/ch/{channel.slug}/programs/new/",
        data={
            "title": "自動算出B",
            "type": "recorded",
            "start_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "asset": asset_ready.id,
            "public_visible": "on",
            "vod_visibility": "off",
        },
    )
    assert res.status_code == 302
    prog = Program.objects.get(title="自動算出B")
    expected = timedelta(milliseconds=asset_ready.duration_ms + 15_000)
    assert (prog.end_at - prog.start_at) == expected


def test_program_create_requires_end_when_no_asset(staff_client, channel):
    """素材の無い番組 (live など) で終了時刻が空なら自動算出できずエラー (作成されない)。"""
    from datetime import timedelta

    from django.utils import timezone

    from scheduling.models import Program

    start = (timezone.now() + timedelta(hours=1)).replace(microsecond=0)
    res = staff_client.post(
        f"/scheduling/ch/{channel.slug}/programs/new/",
        data={
            "title": "終了未指定",
            "type": "live",
            "start_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "public_visible": "on",
            "vod_visibility": "off",
        },
    )
    assert res.status_code == 200  # フォーム再描画
    assert not Program.objects.filter(title="終了未指定").exists()


def test_admin_root_redirects(http_client, db):
    # 未認証は /admin/ → login へリダイレクト (staff_client だと 200 になるため http_client)
    res = http_client.get("/admin/")
    assert res.status_code in (301, 302)


# ---- 番組編集 / 削除 ----


def _make_program(channel, asset_ready):
    from datetime import timedelta

    from django.utils import timezone

    from scheduling.models import Program, ProgramType

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="番組A",
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        asset=asset_ready,
        public_visible=True,
    )


def test_program_update_get_renders_edit_form(staff_client, channel, asset_ready):
    prog = _make_program(channel, asset_ready)
    res = staff_client.get(f"/scheduling/ch/{channel.slug}/programs/{prog.id}/edit/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "番組編集" in body
    assert "番組A" in body


def test_program_update_post_modifies_title(staff_client, channel, asset_ready):
    from scheduling.models import Program

    prog = _make_program(channel, asset_ready)
    res = staff_client.post(
        f"/scheduling/ch/{channel.slug}/programs/{prog.id}/edit/",
        data={
            "title": "番組A (改)",
            "type": "recorded",
            "start_at": prog.start_at.strftime("%Y-%m-%dT%H:%M"),
            "end_at": prog.end_at.strftime("%Y-%m-%dT%H:%M"),
            "asset": asset_ready.id,
            "public_visible": "on",
            "vod_visibility": "off",  # ProgramForm の必須選択 (描画 <select> は常に送出)
        },
    )
    assert res.status_code == 302
    prog.refresh_from_db()
    assert Program.objects.get(pk=prog.id).title == "番組A (改)"


def test_program_update_other_channel_404(staff_client, db, asset_ready):
    from core.models import Channel

    ch1 = Channel.objects.create(name="ch1", slug="ch1", enabled=True)
    ch2 = Channel.objects.create(name="ch2", slug="ch2", enabled=True)
    prog = _make_program(ch2, asset_ready)
    # ch1 経由で ch2 の program を編集しようとして 404
    res = staff_client.get(f"/scheduling/ch/{ch1.slug}/programs/{prog.id}/edit/")
    assert res.status_code == 404


def test_program_delete_removes_row(staff_client, channel, asset_ready):
    from scheduling.models import Program

    prog = _make_program(channel, asset_ready)
    res = staff_client.post(f"/scheduling/ch/{channel.slug}/programs/{prog.id}/delete/")
    assert res.status_code == 302
    assert not Program.objects.filter(pk=prog.id).exists()


def test_program_delete_requires_post(staff_client, channel, asset_ready):
    prog = _make_program(channel, asset_ready)
    res = staff_client.get(f"/scheduling/ch/{channel.slug}/programs/{prog.id}/delete/")
    assert res.status_code == 405
