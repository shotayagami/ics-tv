# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""内部エンドポイント /api/v1/internal/breaking-telop の単体テスト。

地震速報サブシステム (別リポ icstv-earthquake) が速報テロップ (layer40) を発射する経路。
X-Internal-Token 認証 + show(show+future hide)/clear + text 必須を検証する。
"""

from __future__ import annotations

import json

import pytest

from playout.models import PlayoutAction, PlayoutEvent

URL = "/api/v1/internal/breaking-telop"
TOK = "eq-fire-secret"


def _post(client, body, token=TOK):
    headers = {"HTTP_X_INTERNAL_TOKEN": token} if token is not None else {}
    return client.post(URL, data=json.dumps(body), content_type="application/json", **headers)


@pytest.mark.django_db
def test_show_fires_layer40_show_and_future_hide(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"text": "緊急地震速報　強い揺れに警戒", "duration_sec": 120})
    assert r.status_code == 200
    assert r.json()["fired"] == ["ch1"]

    evs = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.OVERLAY_OP)
    show = evs.get(params__overlay_op="show")
    hide = evs.get(params__overlay_op="hide")
    assert show.params["overlay_layer"] == "40"
    assert "強い揺れに警戒" in show.params["overlay_data"]
    assert 110 <= (hide.scheduled_at - show.scheduled_at).total_seconds() <= 130


@pytest.mark.django_db
def test_requires_valid_token(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"text": "x"}, token=None).status_code == 401
    assert _post(client, {"text": "x"}, token="wrong").status_code == 401
    assert PlayoutEvent.objects.count() == 0


@pytest.mark.django_db
def test_clear_fires_clear(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"op": "clear"})
    assert r.status_code == 200
    ev = PlayoutEvent.objects.get(action=PlayoutAction.OVERLAY_OP)
    assert ev.params["overlay_op"] == "clear"


@pytest.mark.django_db
def test_show_requires_text(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"op": "show"}).status_code == 400
    assert PlayoutEvent.objects.count() == 0


@pytest.mark.django_db
def test_token_unset_is_401(client, channel, settings):
    settings.EARTHQUAKE_FIRE_TOKEN = ""  # 未設定 = 機能無効
    assert _post(client, {"text": "x"}, token="anything").status_code == 401


@pytest.mark.django_db
def test_chime_fires_layer41_play_and_future_stop(client, channel, settings):
    """chime=eew で速報テロップ(40)に加え層41の音声 PLAY/STOP を併発する。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r = _post(client, {"text": "緊急地震速報", "chime": "eew"})
    assert r.status_code == 200

    chimes = PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.OVERLAY_OP, params__overlay_layer="41"
    )
    show = chimes.get(params__overlay_op="show")
    stop = chimes.get(params__overlay_op="hide")
    assert show.params["overlay_kind"] == "video"
    assert show.params["overlay_clip"] == "sfx/eew"
    assert stop.scheduled_at > show.scheduled_at
    # テロップ層(40)はそのまま出る
    assert PlayoutEvent.objects.filter(params__overlay_layer="40").exists()


@pytest.mark.django_db
def test_no_chime_by_default(client, channel, settings):
    """chime 未指定なら層41は一切発火しない(従来互換・無音)。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"text": "x"}).status_code == 200
    assert not PlayoutEvent.objects.filter(params__overlay_layer="41").exists()


@pytest.mark.django_db
def test_unknown_chime_is_noop(client, channel, settings):
    """未登録カテゴリは no-op(層41を出さない)。clip 解決不能ゆえ無音。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"text": "x", "chime": "bogus"}).status_code == 200
    assert not PlayoutEvent.objects.filter(params__overlay_layer="41").exists()


@pytest.mark.django_db
def test_clear_does_not_chime(client, channel, settings):
    """op=clear では chime を指定しても鳴らさない。"""
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    assert _post(client, {"op": "clear", "chime": "eew"}).status_code == 200
    assert not PlayoutEvent.objects.filter(params__overlay_layer="41").exists()


@pytest.mark.django_db
def test_second_show_cancels_first_hide(client, channel, settings):
    """2件目の show 到着で 1件目の hide が CANCELLED になり、2件目の hide だけが SCHEDULED に残る。

    地震速報サブシステムが同一ポーリングサイクルで 2件 show を連投したとき、先発テロップの
    自動クリアタイマー(hide)が残存して後発テロップのタイマーと重複・衝突しないことを検証する。
    """
    from playout.models import PlayoutStatus

    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    r1 = _post(client, {"text": "震度速報　強い揺れに警戒", "duration_sec": 90})
    assert r1.status_code == 200

    r2 = _post(client, {"text": "地震情報　最大震度5強", "duration_sec": 90})
    assert r2.status_code == 200

    hides = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_op="hide",
        params__overlay_layer="40",
    )
    # 先発 hide は CANCELLED、後発 hide だけ SCHEDULED
    assert hides.filter(status=PlayoutStatus.SCHEDULED).count() == 1
    assert hides.filter(status=PlayoutStatus.CANCELLED).count() == 1

    scheduled_hide = hides.get(status=PlayoutStatus.SCHEDULED)
    show2 = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_op="show",
        params__overlay_layer="40",
        params__overlay_data__icontains="地震情報",
    ).first()
    assert show2 is not None
    # 後発 show の scheduled_at から ~90 秒後に hide が来ること
    delta = (scheduled_hide.scheduled_at - show2.scheduled_at).total_seconds()
    assert 80 <= delta <= 100


@pytest.mark.django_db
def test_two_shows_same_chime_both_succeed(client, channel, settings):
    """同一 chime カテゴリで 2件連投しても 500 にならず chime イベントが作成される。

    fire_chime の disc が caller_disc (テロップ本文) を含まない場合、同一マイクロ秒に
    同カテゴリ chime が衝突して IntegrityError → 500 になる回帰を防ぐ。
    """
    settings.EARTHQUAKE_FIRE_TOKEN = TOK
    settings.CHIME_CLIPS = {"general": "sfx/general"}

    r1 = _post(client, {"text": "震度速報　強い揺れに警戒", "duration_sec": 90, "chime": "general"})
    r2 = _post(client, {"text": "地震情報　最大震度5強", "duration_sec": 90, "chime": "general"})
    assert r1.status_code == 200
    assert r2.status_code == 200

    chime_shows = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer="41",
        params__overlay_op="show",
    )
    assert chime_shows.count() == 2
