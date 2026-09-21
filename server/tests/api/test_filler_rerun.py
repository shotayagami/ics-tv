# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""フィラー再放送露出 (Phase A): rerun_eligible 素材をフィラー送出中に「再放送」として出す。

編成 Program が無い (= フィラー) 時間帯でも、素材が rerun_eligible なら現在放送中カード /
プレイヤーへ素材タイトルを「再放送」露出する。既定 (rerun_eligible=False) のフィラーは従来どおり
タイトルを出さない (局ID/プロモ等の混在対策)。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client
from django.utils import timezone

from medialib.models import Asset, AssetKind, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent


def _filler_asset(*, rerun: bool, title: str = "懐かし劇場 #3") -> Asset:
    return Asset.objects.create(
        kind=AssetKind.FILLER,
        title=title,
        duration_ms=600_000,  # 10 分
        r2_key="mezzanine/filler/x.mp4",
        normalize_status=NormalizeStatus.READY,
        rerun_eligible=rerun,
    )


def _emit_filler(channel, asset, *, start, dur_min: int = 10) -> PlayoutEvent:
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=start,
        action=PlayoutAction.PLAY_FILLER,
        asset=asset,
        params={
            "clip": f"filler/{asset.id}",
            "loop": True,
            "until": (start + timedelta(minutes=dur_min)).isoformat(),
        },
    )


def test_rerun_eligible_defaults_false(db):
    """migration 0009 は field 追加のみ (既定 False) = 既存挙動不変。"""
    a = _filler_asset(rerun=False)
    a.refresh_from_db()
    assert a.rerun_eligible is False


def test_on_air_info_rerun_filler(channel):
    from core.now_playing import on_air_info

    now = timezone.now()
    asset = _filler_asset(rerun=True)
    _emit_filler(channel, asset, start=now - timedelta(minutes=2))

    info = on_air_info(channel, now)
    assert info is not None
    assert info["title"] == asset.title
    assert info["is_rerun"] is True
    assert info["start"] is not None and info["end"] is not None
    assert info["end"] > info["start"]


def test_on_air_info_non_eligible_filler_is_none(channel):
    """rerun_eligible=False のフィラーは従来どおりタイトルを出さない (None)。"""
    from core.now_playing import on_air_info

    now = timezone.now()
    asset = _filler_asset(rerun=False)
    _emit_filler(channel, asset, start=now - timedelta(minutes=2))

    assert on_air_info(channel, now) is None


def test_on_air_info_ignores_overlay_op(channel):
    """フィラー上に overlay_op (速報テロップ等) が最新 event で乗っても現在番組は下の本線。

    回帰: on_air_info が action を絞らず最新 event を拾うと overlay_op がタイトル無しで
    「現在番組」を上書きし、実際に流れている再放送フィラーがカードから消えていた。
    """
    from core.now_playing import on_air_info

    now = timezone.now()
    asset = _filler_asset(rerun=True)
    _emit_filler(channel, asset, start=now - timedelta(minutes=2))
    # フィラーより後 (= 最新) に overlay_op を差し込む。
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now - timedelta(minutes=1),
        action=PlayoutAction.OVERLAY_OP,
        params={"layer": 40, "op": "show"},
    )

    info = on_air_info(channel, now)
    assert info is not None, "overlay_op が現在番組を隠してしまっている"
    assert info["title"] == asset.title
    assert info["is_rerun"] is True


def test_home_card_rerun(channel):
    """/api/v1/home: 再放送フィラー送出中はカードに is_rerun + 素材タイトル + 進行区間。"""
    now = timezone.now()
    asset = _filler_asset(rerun=True)
    _emit_filler(channel, asset, start=now - timedelta(minutes=3))

    c = Client().get("/api/v1/home").json()["cards"][0]
    assert c["is_rerun"] is True
    assert c["nowtitle"] == asset.title
    assert isinstance(c["cur_start_ts"], int)
    assert isinstance(c["cur_end_ts"], int)
    assert c["time_range"]  # 編成が無くても再放送クリップ尺で時刻帯を出す


def test_home_card_non_eligible_filler_generic(channel):
    """rerun_eligible=False のフィラーは is_rerun=False かつタイトルを出さない。"""
    now = timezone.now()
    asset = _filler_asset(rerun=False)
    _emit_filler(channel, asset, start=now - timedelta(minutes=3))

    c = Client().get("/api/v1/home").json()["cards"][0]
    assert c["is_rerun"] is False
    assert c["nowtitle"] != asset.title  # 「放送中」/「準備中」


def test_player_current_from_rerun(channel):
    """/api/v1/channels/{slug}: 編成 Program が無くても再放送フィラーを current として露出。"""
    now = timezone.now()
    asset = _filler_asset(rerun=True)
    _emit_filler(channel, asset, start=now - timedelta(minutes=1))

    d = Client().get(f"/api/v1/channels/{channel.slug}").json()
    assert d["current"] is not None
    assert d["current"]["title"] == asset.title
    assert d["current"]["is_rerun"] is True
    assert d["current"]["id"] == 0  # 編成 Program ではない合成行
