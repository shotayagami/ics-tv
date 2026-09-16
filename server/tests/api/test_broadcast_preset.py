# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""配信プリセット (#23) CRUD の HTTP smoke。

tags のカンマ区切り↔list 変換、手動チェックリストの default 反映を確認する。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def test_preset_list_unauthenticated_redirects_to_login(http_client):
    res = http_client.get("/admin-ui/youtube/presets/")
    assert res.status_code == 302
    assert "login" in res.url


def test_preset_list_staff_renders_ok(staff_client):
    res = staff_client.get("/admin-ui/youtube/presets/")
    assert res.status_code == 200
    assert "配信プリセット" in res.content.decode("utf-8")


def test_preset_create_parses_tags_and_checklist(staff_client, channel):
    from youtube.models import YoutubeBroadcastPreset

    res = staff_client.post(
        "/admin-ui/youtube/presets/new/",
        {
            "name": "VRChat 生配信",
            "channel": "",  # 全ch共通
            "title_template": "【#VRChat】{program}",
            "description_template": "八神翔太です。",
            "category_id": "24",  # エンターテイメント
            "privacy": "public",
            "default_language": "ja",
            "default_audio_language": "ja",
            "latency": "low",
            "license": "youtube",
            "tags": "VRChat, ゲーム実況 , ,アイシーエス",
            "playlist_id": "",
            # 手動チェックリスト: 1 項目だけ既定 ON
            "checklist_ai_disclosure": "1",
        },
    )
    assert res.status_code == 302  # 作成後 edit へ redirect
    p = YoutubeBroadcastPreset.objects.get(name="VRChat 生配信")
    assert p.channel is None
    assert p.category_id == 24
    assert p.tags == ["VRChat", "ゲーム実況", "アイシーエス"]  # 空要素は除去・trim
    # checklist: ai_disclosure のみ True、他は False
    by_key = {c["key"]: c["default"] for c in p.manual_checklist}
    assert by_key["ai_disclosure"] is True
    assert by_key["paid_promotion"] is False
    assert "age_restriction" in by_key  # 定義一式が保持されている


def test_preset_edit_updates_fields(staff_client):
    from youtube.models import YoutubeBroadcastPreset

    p = YoutubeBroadcastPreset.objects.create(name="old", tags=["a"])
    res = staff_client.post(
        f"/admin-ui/youtube/presets/{p.id}/edit/",
        {
            "name": "new",
            "channel": "",
            "title_template": "",
            "description_template": "",
            "category_id": "",
            "privacy": "unlisted",
            "default_language": "",
            "default_audio_language": "",
            "latency": "ultraLow",
            "license": "youtube",
            "tags": "x, y",
            "playlist_id": "",
        },
    )
    assert res.status_code == 302
    p.refresh_from_db()
    assert p.name == "new"
    assert p.privacy == "unlisted"
    assert p.latency == "ultraLow"
    assert p.category_id is None
    assert p.tags == ["x", "y"]


def test_preset_delete(staff_client):
    from youtube.models import YoutubeBroadcastPreset

    p = YoutubeBroadcastPreset.objects.create(name="todelete")
    res = staff_client.post(f"/admin-ui/youtube/presets/{p.id}/delete/")
    assert res.status_code == 302
    assert not YoutubeBroadcastPreset.objects.filter(pk=p.id).exists()
