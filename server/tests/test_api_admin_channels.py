# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-5): チャンネル管理 admin API。

staff_auth ゲート + 一覧 + name/slug/略称/識別色/公開 の編集と検証 (既存 channel_update と同等)。
"""

from __future__ import annotations

import json

from core.models import Channel

_LIST = "/api/v1/admin/channels"


def _post(client, slug, payload):
    return client.post(
        f"/api/v1/admin/channels/{slug}", data=json.dumps(payload), content_type="application/json"
    )


# ---- 認可 ----


def test_list_requires_auth(http_client, db):
    assert http_client.get(_LIST).status_code == 401


def test_update_requires_auth(http_client, channel, db):
    assert _post(http_client, channel.slug, {"name": "x", "slug": channel.slug}).status_code in (
        401,
        403,
    )


# ---- 一覧 ----


def test_list_returns_channels(staff_client, channel, db):
    d = staff_client.get(_LIST).json()
    row = next(c for c in d if c["slug"] == channel.slug)
    assert row["name"] == channel.name
    assert row["tint_color"].startswith("#") and len(row["tint_color"]) == 7  # 解決済み
    assert row["settings_url"].endswith(f"/admin-ui/channels/{channel.slug}/settings/")


# ---- 編集 ----


def test_update_name_short_tint_enabled(staff_client, channel, db):
    r = _post(
        staff_client,
        channel.slug,
        {
            "name": "新総合",
            "slug": channel.slug,
            "short": "総合",
            "tint": "#1982c4",
            "enabled": False,
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    channel.refresh_from_db()
    assert channel.name == "新総合" and channel.short == "総合"
    assert channel.tint == "#1982c4" and channel.enabled is False


def test_update_empty_name_400(staff_client, channel, db):
    assert (
        _post(staff_client, channel.slug, {"name": "  ", "slug": channel.slug}).status_code == 400
    )


def test_update_bad_tint_400(staff_client, channel, db):
    assert (
        _post(
            staff_client, channel.slug, {"name": "x", "slug": channel.slug, "tint": "red"}
        ).status_code
        == 400
    )


def test_update_bad_slug_400(staff_client, channel, db):
    assert _post(staff_client, channel.slug, {"name": "x", "slug": "bad slug!"}).status_code == 400


def test_update_dup_slug_400(staff_client, channel, db):
    Channel.objects.create(name="2ch", slug="ch2", enabled=True)
    assert _post(staff_client, channel.slug, {"name": "x", "slug": "ch2"}).status_code == 400


def test_update_slug_rename(staff_client, channel, db):
    r = _post(staff_client, channel.slug, {"name": channel.name, "slug": "renamed"})
    assert r.status_code == 200
    assert Channel.objects.filter(slug="renamed").exists()


# ---- 詳細設定 (#2e-4) ----


def test_settings_requires_auth(http_client, channel, db):
    assert http_client.get(f"{_LIST}/{channel.slug}/settings").status_code == 401


def test_settings_structure(staff_client, channel, db):
    from medialib.models import (
        Asset,
        AssetKind,
        FillerPlaylist,
        NormalizeStatus,
    )

    fp = FillerPlaylist.objects.create(name="局ID")
    slate = Asset.objects.create(
        kind=AssetKind.FILLER,
        title="スレートA",
        duration_ms=5000,
        normalize_status=NormalizeStatus.READY,
    )
    # 番組/CM は スレート候補に出ない。未正規化も出ない。
    Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="番組X",
        duration_ms=1000,
        normalize_status=NormalizeStatus.READY,
    )
    channel.default_filler = fp
    channel.slate_asset = slate
    channel.cf_live_input_id = "cf-input-123"
    channel.save()

    d = staff_client.get(f"{_LIST}/{channel.slug}/settings").json()
    assert d["slug"] == channel.slug and d["name"] == channel.name
    assert d["default_filler_id"] == fp.id and d["slate_asset_id"] == slate.id
    assert fp.id in [o["id"] for o in d["filler_options"]]
    slate_ids = [o["id"] for o in d["slate_options"]]
    assert slate.id in slate_ids  # FILLER は候補
    assert all(o["name"] != "番組X" for o in d["slate_options"])  # PROGRAM は除外
    assert d["cloudflare"]["live_input_id"] == "cf-input-123"
    assert d["youtube"]["connect_url"].endswith(f"/admin-ui/ch/{channel.slug}/youtube/connect/")
    assert d["youtube"]["connected"] is False  # 未接続
    assert d["media_post_url"].endswith(f"/admin-ui/ch/{channel.slug}/media/")
    assert d["advanced_url"].endswith(f"/admin-ui/ch/{channel.slug}/")
