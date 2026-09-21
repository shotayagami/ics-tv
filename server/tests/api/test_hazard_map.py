# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""内部エンドポイント /api/v1/internal/hazard-map の単体テスト。

津波沿岸/震度のフルスクリーン地図 (layer38・kind=graphic) を発射する経路。
X-Internal-Token 認証 + show/update/clear + kind/op バリデーション + auto-hide の張り直しを検証。
"""

from __future__ import annotations

import json

import pytest

from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus

URL = "/api/v1/internal/hazard-map"
TOK = "eq-fire-secret"


def _post(client, body, token=TOK):
    headers = {"HTTP_X_INTERNAL_TOKEN": token} if token is not None else {}
    return client.post(URL, data=json.dumps(body), content_type="application/json", **headers)


@pytest.mark.django_db
def test_show_fires_layer38_graphic_and_future_hide(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    data = {"maxGrade": "major", "areas": [{"code": "220", "grade": "major"}]}
    r = _post(client, {"kind": "tsunami", "data": data, "duration_sec": 120})
    assert r.status_code == 200
    assert r.json()["fired"] == ["ch1"]

    evs = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.OVERLAY_OP)
    show = evs.get(params__overlay_op="show")
    hide = evs.get(params__overlay_op="hide")
    assert show.params["overlay_layer"] == "38"
    assert show.params["overlay_kind"] == "graphic"
    assert show.params["overlay_template"] == "map/tsunami"
    assert "major" in show.params["overlay_data"]
    assert 110 <= (hide.scheduled_at - show.scheduled_at).total_seconds() <= 130
    assert hide.params["overlay_layer"] == "38"


@pytest.mark.django_db
def test_seismic_uses_seismic_template(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"kind": "seismic", "data": {"maxInt": "5+", "stations": []}})
    assert r.status_code == 200
    show = PlayoutEvent.objects.get(action=PlayoutAction.OVERLAY_OP, params__overlay_op="show")
    assert show.params["overlay_template"] == "map/seismic"


@pytest.mark.django_db
def test_seismic_zoom_uses_seismic_zoom_template(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"kind": "seismic_zoom", "data": {"maxInt": "5+", "stations": []}})
    assert r.status_code == 200
    show = PlayoutEvent.objects.get(action=PlayoutAction.OVERLAY_OP, params__overlay_op="show")
    assert show.params["overlay_layer"] == "38"
    assert show.params["overlay_template"] == "map/seismic-zoom"


@pytest.mark.django_db
def test_seismic_regional_uses_seismic_regional_template(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"kind": "seismic_regional", "data": {"maxInt": "5+", "stations": []}})
    assert r.status_code == 200
    show = PlayoutEvent.objects.get(action=PlayoutAction.OVERLAY_OP, params__overlay_op="show")
    assert show.params["overlay_layer"] == "38"
    assert show.params["overlay_template"] == "map/seismic-regional"


@pytest.mark.django_db
def test_update_streams_data_without_template(client, channel, settings):
    """op=update は同一地図へ CG UPDATE で続報を流す (テンプレ再ADDしない)。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"kind": "tsunami", "op": "update", "data": {"areas": []}})
    assert r.status_code == 200
    upd = PlayoutEvent.objects.get(action=PlayoutAction.OVERLAY_OP, params__overlay_op="update")
    assert upd.params["overlay_layer"] == "38"
    assert "overlay_template" not in upd.params  # UPDATE はテンプレ不要
    assert upd.params["overlay_kind"] == "graphic"


@pytest.mark.django_db
def test_clear_fires_clear_and_cancels_hide(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    _post(client, {"kind": "tsunami", "data": {"areas": []}})  # show → 1 hide scheduled
    r = _post(client, {"kind": "tsunami", "op": "clear"})
    assert r.status_code == 200
    clr = PlayoutEvent.objects.get(action=PlayoutAction.OVERLAY_OP, params__overlay_op="clear")
    assert clr.params["overlay_layer"] == "38"
    # show が張った hide は CANCELLED
    assert (
        PlayoutEvent.objects.filter(
            params__overlay_op="hide",
            params__overlay_layer="38",
            status=PlayoutStatus.SCHEDULED,
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_update_rearms_hide(client, channel, settings):
    """show → update と続くと、先の hide は CANCELLED、update 起点の hide だけ SCHEDULED に残る
    (続報が続く限り地図が消えない設計)。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    _post(client, {"kind": "tsunami", "data": {"areas": []}, "duration_sec": 120})
    _post(client, {"kind": "tsunami", "op": "update", "data": {"areas": []}, "duration_sec": 120})
    hides = PlayoutEvent.objects.filter(
        channel=channel, params__overlay_op="hide", params__overlay_layer="38"
    )
    assert hides.filter(status=PlayoutStatus.SCHEDULED).count() == 1
    assert hides.filter(status=PlayoutStatus.CANCELLED).count() == 1


@pytest.mark.django_db
def test_tsunami_corner_uses_layer37_independent_of_layer38(client, channel, settings):
    """tsunami_corner (常時表示ミニマップ) は layer37 固定・map/tsunami-corner を ADD する。
    layer38 のフルスクリーン地図 (tsunami) と同時に show しても互いの hide を取り消し合わない。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    _post(client, {"kind": "tsunami", "data": {"areas": []}, "duration_sec": 120})
    r = _post(client, {"kind": "tsunami_corner", "data": {"areas": []}, "duration_sec": 120})
    assert r.status_code == 200
    assert r.json()["fired"] == ["ch1"]

    corner_show = PlayoutEvent.objects.get(
        action=PlayoutAction.OVERLAY_OP, params__overlay_op="show", params__overlay_layer="37"
    )
    assert corner_show.params["overlay_template"] == "map/tsunami-corner"
    assert corner_show.params["overlay_kind"] == "graphic"

    # layer38 (先の tsunami) の hide は layer37 発火の影響を受けず SCHEDULED のまま。
    assert (
        PlayoutEvent.objects.filter(
            params__overlay_op="hide", params__overlay_layer="38", status=PlayoutStatus.SCHEDULED
        ).count()
        == 1
    )
    assert (
        PlayoutEvent.objects.filter(
            params__overlay_op="hide", params__overlay_layer="37", status=PlayoutStatus.SCHEDULED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_tsunami_corner_clear_only_affects_layer37(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    _post(client, {"kind": "tsunami", "data": {"areas": []}})
    _post(client, {"kind": "tsunami_corner", "data": {"areas": []}})
    r = _post(client, {"kind": "tsunami_corner", "op": "clear"})
    assert r.status_code == 200
    clr = PlayoutEvent.objects.get(
        action=PlayoutAction.OVERLAY_OP, params__overlay_op="clear", params__overlay_layer="37"
    )
    assert clr.params["overlay_layer"] == "37"
    # layer37 の hide は取り消し済み、layer38 の hide (tsunami 側) は影響を受けず残る。
    assert (
        PlayoutEvent.objects.filter(
            params__overlay_op="hide", params__overlay_layer="37", status=PlayoutStatus.SCHEDULED
        ).count()
        == 0
    )
    assert (
        PlayoutEvent.objects.filter(
            params__overlay_op="hide", params__overlay_layer="38", status=PlayoutStatus.SCHEDULED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_requires_valid_token(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"kind": "tsunami"}, token=None).status_code == 401
    assert _post(client, {"kind": "tsunami"}, token="wrong").status_code == 401
    assert PlayoutEvent.objects.count() == 0


@pytest.mark.django_db
def test_invalid_kind_and_op_are_400(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"kind": "volcano"}).status_code == 400
    assert _post(client, {"kind": "tsunami", "op": "bogus"}).status_code == 400
    assert PlayoutEvent.objects.count() == 0
