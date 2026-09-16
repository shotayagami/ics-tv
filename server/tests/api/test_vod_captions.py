# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""見逃し字幕 (#PLAYER-04・決定#24): 同一オリジン VTT 配信のゲート + 詳細テンプレの track 配線。"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from medialib.models import CaptionStatus
from members.models import Member
from scheduling.models import Program, ProgramType, VodVisibility

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_VTT = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n\xe3\x81\x8a\xe3\x81\xaf\xe3\x82\x88\n"


def _member(email="m@example.com", verified=True):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _program(channel, asset, *, visibility=VodVisibility.PUBLIC, ended_ago=timedelta(hours=2)):
    end = timezone.now() - ended_ago
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="放送済み番組",
        start_at=end - timedelta(hours=1),
        end_at=end,
        asset=asset,
        vod_visibility=visibility,
    )


def _with_caption(asset):
    asset.caption_status = CaptionStatus.READY
    asset.caption_r2_key = f"captions/program/{asset.id}.ja.vtt"
    asset.save(update_fields=["caption_status", "caption_r2_key"])
    return asset


# ---- 配信ビュー ----


@_PUBLIC_HOST
def test_captions_public_serves_vtt(http_client, channel, asset_ready, db):
    p = _program(channel, _with_caption(asset_ready))
    with mock.patch("core.r2.get_object", return_value=(_VTT, "text/vtt")) as go:
        res = http_client.get(f"/vod/{p.id}/captions.vtt")
    assert res.status_code == 200
    assert res["Content-Type"].startswith("text/vtt")
    assert res["Cache-Control"] == "no-store"
    assert res.content == _VTT
    go.assert_called_once_with(asset_ready.caption_r2_key)


@_PUBLIC_HOST
def test_captions_missing_is_404(http_client, channel, asset_ready, db):
    # caption_status=none のまま = 字幕未生成
    p = _program(channel, asset_ready)
    assert http_client.get(f"/vod/{p.id}/captions.vtt").status_code == 404


@_PUBLIC_HOST
def test_captions_off_program_is_404(http_client, channel, asset_ready, db):
    p = _program(channel, _with_caption(asset_ready), visibility=VodVisibility.OFF)
    assert http_client.get(f"/vod/{p.id}/captions.vtt").status_code == 404


@_PUBLIC_HOST
def test_captions_members_anon_is_404(http_client, channel, asset_ready, db):
    # 詳細はログインへ 302 だが、字幕サブリソースは存在を漏らさず一律 404。
    p = _program(channel, _with_caption(asset_ready), visibility=VodVisibility.MEMBERS)
    assert http_client.get(f"/vod/{p.id}/captions.vtt").status_code == 404


@_PUBLIC_HOST
def test_captions_members_verified_serves(http_client, channel, asset_ready, db):
    _member(verified=True)
    _login(http_client)
    p = _program(channel, _with_caption(asset_ready), visibility=VodVisibility.MEMBERS)
    with mock.patch("core.r2.get_object", return_value=(_VTT, "text/vtt")):
        res = http_client.get(f"/vod/{p.id}/captions.vtt")
    assert res.status_code == 200 and res.content == _VTT


# ---- 詳細テンプレの <track> 配線 ----


@_PUBLIC_HOST
def test_detail_wires_captions_track_when_present(http_client, channel, asset_ready, db):
    p = _program(channel, _with_caption(asset_ready))
    with mock.patch("core.r2.presign_get", return_value="https://r2/signed.mp4"):
        body = http_client.get(f"/vod/{p.id}/").content.decode("utf-8")
    assert f'data-captions="/vod/{p.id}/captions.vtt"' in body


@_PUBLIC_HOST
def test_detail_omits_captions_track_when_absent(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready)  # 字幕なし
    with mock.patch("core.r2.presign_get", return_value="https://r2/signed.mp4"):
        body = http_client.get(f"/vod/{p.id}/").content.decode("utf-8")
    assert "data-captions" not in body
