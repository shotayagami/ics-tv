# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2e-2): 番組 作成/編集フォーム admin API。

staff_auth ゲート + フォーム選択肢(素材/live_source) + 作成/更新 (既存 ProgramForm の検証を再利用)。
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.utils import timezone

from scheduling.models import Program, ProgramType


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def test_program_form_requires_auth(http_client, channel, db):
    assert (
        http_client.get(f"/api/v1/admin/scheduling/{channel.slug}/program-form").status_code == 401
    )


def test_program_form_create_options(staff_client, channel, asset_ready, db):
    d = staff_client.get(
        f"/api/v1/admin/scheduling/{channel.slug}/program-form?start=2026-07-01T20:00:00&end=2026-07-01T21:00:00"
    ).json()
    assert any(a["id"] == asset_ready.id for a in d["assets"])
    assert d["initial"]["start_at"] == "2026-07-01T20:00:00" and d["initial"]["id"] is None
    assert d["delete_url"] == ""


def test_program_form_options_filter_by_usable_as_program(staff_client, channel, db):
    """番組ピッカーは usable_as_program で絞る: フラグを立てた filler 素材は候補に出る (「フィラーかつ番組」)。"""
    from medialib.models import Asset, AssetKind, NormalizeStatus

    plain = Asset.objects.create(
        kind=AssetKind.FILLER, title="ただのフィラー", normalize_status=NormalizeStatus.READY
    )
    dual = Asset.objects.create(
        kind=AssetKind.FILLER, title="番組兼用フィラー", normalize_status=NormalizeStatus.READY
    )
    dual.usable_as_program = True
    dual.save()

    ids = {
        a["id"]
        for a in staff_client.get(f"/api/v1/admin/scheduling/{channel.slug}/program-form").json()[
            "assets"
        ]
    }
    assert dual.id in ids
    assert plain.id not in ids


def test_create_recorded_program(staff_client, channel, asset_ready, db):
    r = _post(
        staff_client,
        f"/api/v1/admin/scheduling/{channel.slug}/programs",
        {
            "title": "新番組",
            "type": "recorded",
            "start_at": "2026-07-01T20:00:00",
            "end_at": "2026-07-01T21:00:00",
            "asset_id": asset_ready.id,
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert Program.objects.filter(channel=channel, title="新番組", asset=asset_ready).exists()


def test_create_recorded_without_asset_400(staff_client, channel, db):
    r = _post(
        staff_client,
        f"/api/v1/admin/scheduling/{channel.slug}/programs",
        {
            "title": "x",
            "type": "recorded",
            "start_at": "2026-07-01T20:00:00",
            "end_at": "2026-07-01T21:00:00",
        },
    )
    assert r.status_code == 400  # 録画は素材必須 (ProgramForm.clean)


def test_program_form_edit_and_update(staff_client, channel, asset_ready, db):
    now = timezone.now()
    p = Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="旧タイトル",
        start_at=now + timedelta(days=1),
        end_at=now + timedelta(days=1, hours=1),
        asset=asset_ready,
    )
    d = staff_client.get(
        f"/api/v1/admin/scheduling/{channel.slug}/program-form?program_id={p.id}"
    ).json()
    assert d["initial"]["id"] == p.id and d["initial"]["title"] == "旧タイトル"
    assert d["delete_url"].endswith(f"/programs/{p.id}/delete/")
    r = _post(
        staff_client,
        f"/api/v1/admin/scheduling/{channel.slug}/programs/{p.id}",
        {
            "title": "新タイトル",
            "type": "recorded",
            "start_at": d["initial"]["start_at"],
            "end_at": d["initial"]["end_at"],
            "asset_id": asset_ready.id,
        },
    )
    assert r.status_code == 200
    p.refresh_from_db()
    assert p.title == "新タイトル"
