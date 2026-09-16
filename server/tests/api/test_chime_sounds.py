# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""速報チャイム音源ライブラリ (ChimeSound) + カテゴリ別選択 (ChannelChime) の管理 / 配布 / 発火解決。

R2 は monkeypatch でスタブ化 (put_object/presign_get/delete_object)。送出ノードへの実配布は standing
prefetch manifest 経由 (agent が pin+DL) のため、ここではサーバ側の結線のみ検証する。
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core.models import ChannelChime, ChimeSound
from core.views import fire_chime
from playout.grpc_service import _build_prefetch_manifest
from playout.models import PlayoutAction, PlayoutEvent


@pytest.fixture
def r2_stub(monkeypatch):
    """R2 を捕捉するスタブ。put/delete を記録し presign は決定論的 URL を返す。"""
    calls = {"put": [], "deleted": []}
    monkeypatch.setattr("core.r2.put_object", lambda key, body, ct: calls["put"].append((key, ct)))
    monkeypatch.setattr("core.r2.delete_object", lambda key: calls["deleted"].append(key))
    monkeypatch.setattr("core.r2.presign_get", lambda key, expires=600: f"https://signed/{key}")
    return calls


def _audio(name="chime.mp3", ct="audio/mpeg"):
    return SimpleUploadedFile(name, b"ID3\x00\x00fakeaudio", content_type=ct)


def _add(staff_client, name="ベル", f=None):
    return staff_client.post("/admin-ui/chime/sound/", {"name": name, "file": f or _audio()})


# ---- ライブラリ管理 ----


@pytest.mark.django_db
def test_upload_adds_to_library(staff_client, r2_stub):
    res = _add(staff_client, "EEW チャイム")
    assert res.status_code == 200
    s = ChimeSound.objects.get()
    assert s.name == "EEW チャイム"
    assert s.r2_key.startswith("chime/lib/") and s.r2_key.endswith(".mp3")
    assert s.clip == s.r2_key.rsplit(".", 1)[0]
    assert len(r2_stub["put"]) == 1


@pytest.mark.django_db
def test_upload_rejects_non_audio(staff_client, r2_stub):
    res = staff_client.post(
        "/admin-ui/chime/sound/",
        {"name": "x", "file": SimpleUploadedFile("x.png", b"\x89PNG", content_type="image/png")},
    )
    assert res.status_code == 400
    assert not ChimeSound.objects.exists()
    assert not r2_stub["put"]


@pytest.mark.django_db
def test_delete_library_sound_clears_selection(staff_client, channel, r2_stub):
    _add(staff_client)
    s = ChimeSound.objects.get()
    ChannelChime.objects.create(channel=channel, category="eew", sound=s)
    res = staff_client.post(f"/admin-ui/chime/sound/{s.id}/delete/")
    assert res.status_code == 200
    assert not ChimeSound.objects.exists()
    assert s.r2_key in r2_stub["deleted"]
    # SET_NULL: 選択は残るが sound は外れる (未選択 → 既定フォールバック)
    sel = ChannelChime.objects.get(channel=channel, category="eew")
    assert sel.sound_id is None


# ---- カテゴリ選択 ----


@pytest.mark.django_db
def test_select_category_sound(staff_client, channel, r2_stub):
    _add(staff_client)
    s = ChimeSound.objects.get()
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/chime/select/", {"category": "weather", "sound": str(s.id)}
    )
    assert res.status_code == 200
    assert ChannelChime.objects.get(channel=channel, category="weather").sound_id == s.id


@pytest.mark.django_db
def test_select_empty_clears(staff_client, channel, r2_stub):
    _add(staff_client)
    s = ChimeSound.objects.get()
    ChannelChime.objects.create(channel=channel, category="general", sound=s)
    res = staff_client.post(
        f"/admin-ui/ch/{channel.slug}/chime/select/", {"category": "general", "sound": ""}
    )
    assert res.status_code == 200
    assert ChannelChime.objects.get(channel=channel, category="general").sound_id is None


# ---- 発火解決 (fire_chime) ----


@pytest.mark.django_db
def test_fire_chime_uses_category_selection(channel):
    s = ChimeSound.objects.create(name="b", r2_key="chime/lib/abcd.mp3")
    ChannelChime.objects.create(channel=channel, category="eew", sound=s)
    fire_chime([channel], "eew")
    show = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.OVERLAY_OP, params__overlay_op="show"
    )
    assert show.params["overlay_layer"] == "41"
    assert show.params["overlay_clip"] == "chime/lib/abcd"


@pytest.mark.django_db
def test_fire_chime_sound_id_overrides_category(channel):
    """発火時 sound_id を渡すとカテゴリ選択より優先 (手動速報のその場選択)。"""
    cat_sound = ChimeSound.objects.create(name="cat", r2_key="chime/lib/cat.mp3")
    ChannelChime.objects.create(channel=channel, category="eew", sound=cat_sound)
    pick = ChimeSound.objects.create(name="pick", r2_key="chime/lib/pick.wav")
    fire_chime([channel], "eew", sound_id=pick.id)
    show = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.OVERLAY_OP, params__overlay_op="show"
    )
    assert show.params["overlay_clip"] == "chime/lib/pick"


@pytest.mark.django_db
def test_fire_chime_falls_back_to_settings_default(channel):
    """カテゴリ未選択なら settings/既定 (sfx/eew) にフォールバック。"""
    fire_chime([channel], "eew")
    show = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.OVERLAY_OP, params__overlay_op="show"
    )
    assert show.params["overlay_clip"] == "sfx/eew"


# ---- 配布 (manifest) ----


@pytest.mark.django_db
def test_manifest_pins_whole_library(channel, r2_stub):
    ChimeSound.objects.create(name="a", r2_key="chime/lib/aa.mp3")
    ChimeSound.objects.create(name="b", r2_key="chime/lib/bb.wav")
    manifest = _build_prefetch_manifest(channel)
    clips = {m["clip"] for m in manifest}
    assert {"chime/lib/aa", "chime/lib/bb"} <= clips
    assert all(m["url"].startswith("https://signed/") for m in manifest)


# ---- settings / ops エンドポイント ----


@pytest.mark.django_db
def test_settings_endpoint_lists_library_and_selection(staff_client, channel, r2_stub):
    _add(staff_client, "ベルA")
    s = ChimeSound.objects.get()
    ChannelChime.objects.create(channel=channel, category="eew", sound=s)
    res = staff_client.get(f"/api/v1/admin/channels/{channel.slug}/settings")
    assert res.status_code == 200
    body = res.json()
    assert [x["name"] for x in body["chime_library"]] == ["ベルA"]
    chimes = {c["category"]: c["selected_sound_id"] for c in body["chimes"]}
    assert chimes == {"eew": s.id, "weather": None, "general": None, "cm_in": None}


@pytest.mark.django_db
def test_ops_status_chime_choices(staff_client, channel, r2_stub):
    ChimeSound.objects.create(name="ベルX", r2_key="chime/lib/x.mp3")
    res = staff_client.get(f"/api/v1/admin/ops/{channel.slug}/status")
    assert res.status_code == 200
    choices = res.json()["chime_choices"]
    cats = [c["value"] for c in choices if c["group"] == "category"]
    libs = [c for c in choices if c["group"] == "library"]
    assert cats == ["cat:eew", "cat:weather", "cat:general", "cat:cm_in"]
    assert any(c["value"].startswith("snd:") and "ベルX" in c["label"] for c in libs)
