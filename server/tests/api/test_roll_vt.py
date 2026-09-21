# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VT cue 送出 (fire_vt_asset/roll_vt_cue/op_roll_vt) のテスト (タイムキープ Phase 1 §6・#25)。

`op_roll_vt` はクリーン実装の新規 PlayoutAction (PLAY_VT) を使う。CM と異なりバンパー
(layer20 OVERLAY_OP) は併発しない — 発火自体が「本編扱い」で CG バンパー/課金台帳の対象外
という #25 のユーザー確定判断そのものを検証する。D9 (直近 CUT_LIVE の cg_* パラメータ引き継ぎ)
が本ファイルの核心。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models import Channel, LiveSource
from core.ops_views import fire_vt_asset
from medialib.models import Asset, AssetKind, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import LiveCue, LiveCueKind, LiveCueState, LiveRundown, Program, ProgramType
from scheduling.resolver import resolve
from scheduling.services import CueNotPendingError, roll_vt_cue

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _url(slug: str, suffix: str) -> str:
    return f"/ops/ch/{slug}/{suffix}"


def _vt_asset(title="VTR1", duration_ms=90_000, r2_key="mezzanine/vt/vtr1.mp4"):
    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title=title,
        duration_ms=duration_ms,
        r2_key=r2_key,
        normalize_status=NormalizeStatus.READY,
    )


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


def _vt_cue(channel, asset, state=LiveCueState.PENDING, rundown=None):
    rundown = rundown or LiveRundown.objects.create(program=_live_program(channel))
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.VT,
        label=asset.title,
        planned_duration_ms=asset.duration_ms,
        asset=asset,
        state=state,
    )


def _vt_auto_fire_cue(channel, asset, *, auto_offset_ms=0, start=BASE):
    """auto_fire=True の VT cue を作る (タイムキープ Phase2 §2 のキャンセルテスト用)。

    呼び出し側が `resolve()` を実行して初めて予約 PlayoutEvent (PLAY_VT 1件) が実発行される
    (Batch A の `_emit_live_auto_fire_cues`)。戻り値: (cue, prog)。
    """
    prog = _live_program(channel, start=start)
    rundown = LiveRundown.objects.create(program=prog)
    cue = LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.VT,
        label=asset.title,
        planned_duration_ms=asset.duration_ms,
        asset=asset,
        state=LiveCueState.PENDING,
        auto_fire=True,
        auto_offset_ms=auto_offset_ms,
    )
    return cue, prog


def _resolve_program_window(prog) -> None:
    resolve(prog.channel, prog.start_at - timedelta(minutes=1), prog.end_at + timedelta(minutes=1))


def _cut_live(channel, prog, params, scheduled_at=None):
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=scheduled_at or prog.start_at,
        action=PlayoutAction.CUT_LIVE,
        status=PlayoutStatus.DONE,
        program=prog,
        params=params,
    )


# ---- 1. fire_vt_asset: PLAY_VT INSERT + バンパー非生成 + cg_* 引き継ぎ ----


@pytest.mark.django_db
def test_fire_vt_asset_inserts_play_vt_with_clip_and_asset(channel):
    asset = _vt_asset(duration_ms=90_000, r2_key="mezzanine/vt/vtr1.mp4")
    ev = fire_vt_asset(channel, asset, discriminator="disc-vt-1")

    assert ev.action == PlayoutAction.PLAY_VT
    assert ev.asset_id == asset.id
    assert ev.params["clip"] == f"asset/{asset.id}"
    assert ev.params["in_ms"] == 0
    assert ev.params["out_ms"] == 90_000
    assert ev.params["r2_key"] == "mezzanine/vt/vtr1.mp4"
    assert ev.params["interrupt"] is True


@pytest.mark.django_db
def test_fire_vt_asset_does_not_create_cm_bumper(channel):
    """CM と違い layer20 OVERLAY_OP バンパーは一切生成されない (#25 ユーザー確定判断)。"""
    asset = _vt_asset()
    fire_vt_asset(channel, asset, discriminator="disc-vt-no-bumper")

    assert not PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.OVERLAY_OP
    ).exists()


@pytest.mark.django_db
def test_fire_vt_asset_copies_cg_params_forward_from_last_cut_live(channel):
    """D9: 直近 CUT_LIVE の cg_* params (Lバー/予告等) と rtmp_url をそのまま引き継ぐこと。
    引き継がないと VT ロール中に L バーが空白になる (docs/timekeeper-live.md D9)。"""
    prog = _live_program(channel)
    _cut_live(
        channel,
        prog,
        {
            "cg_title": "夕方ライブ",
            "cg_channel": "ICS-TV 1ch",
            "cg_preview_title": "次はニュース",
            "rtmp_url": "rtmp://ingest.example/live/ch1",
        },
    )
    asset = _vt_asset()
    ev = fire_vt_asset(channel, asset, discriminator="disc-vt-cg")

    assert ev.params["cg_title"] == "夕方ライブ"
    assert ev.params["cg_channel"] == "ICS-TV 1ch"
    assert ev.params["cg_preview_title"] == "次はニュース"
    assert ev.params["return_rtmp_url"] == "rtmp://ingest.example/live/ch1"
    # rtmp_url 自体 (cut_live 用キー) はそのままではなく return_rtmp_url に転写される
    assert "rtmp_url" not in ev.params


@pytest.mark.django_db
def test_fire_vt_asset_without_prior_cut_live_has_no_cg_params(channel):
    asset = _vt_asset()
    ev = fire_vt_asset(channel, asset, discriminator="disc-vt-no-cutlive")
    assert not any(k.startswith("cg_") for k in ev.params)
    assert "return_rtmp_url" not in ev.params


@pytest.mark.django_db
def test_fire_vt_asset_falls_back_to_rtmp_app_key_when_no_rtmp_url(channel):
    """通常運用の emit_live (scheduling/resolver.py) は rtmp_url ではなく rtmp_app/rtmp_key
    しか params に持たない (agent 側で URL 組み立てさせる設計)。この場合も
    return_rtmp_app/return_rtmp_key を転写し、reel 末尾の自動本線復帰を機能させる
    (このフォールバックが無いと通常運用の CM/VT 明けで自動復帰が一切発火しない)。"""
    prog = _live_program(channel)
    _cut_live(channel, prog, {"cg_title": "生放送", "rtmp_app": "live", "rtmp_key": "ch1"})
    asset = _vt_asset()
    ev = fire_vt_asset(channel, asset, discriminator="disc-vt-app-key")

    assert ev.params["return_rtmp_app"] == "live"
    assert ev.params["return_rtmp_key"] == "ch1"
    assert "return_rtmp_url" not in ev.params


# ---- 2. roll_vt_cue ----


@pytest.mark.django_db
def test_roll_vt_cue_happy_path(channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset)

    result = roll_vt_cue(cue.id)

    assert result.state == LiveCueState.AIRED
    assert result.fired_event_id is not None
    assert result.fired_event.action == PlayoutAction.PLAY_VT
    assert result.fired_event.asset_id == asset.id


@pytest.mark.django_db
def test_roll_vt_cue_double_fire_guard(channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset)
    roll_vt_cue(cue.id)
    with pytest.raises(CueNotPendingError):
        roll_vt_cue(cue.id)


@pytest.mark.django_db
def test_roll_vt_cue_rejects_non_vt_kind(channel):
    rundown = LiveRundown.objects.create(program=_live_program(channel))
    cue = LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.SECTION,
        planned_duration_ms=60_000,
        state=LiveCueState.PENDING,
    )
    with pytest.raises(CueNotPendingError):
        roll_vt_cue(cue.id)


@pytest.mark.django_db
def test_roll_vt_cue_rejects_non_pending(channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset, state=LiveCueState.SKIPPED)
    with pytest.raises(CueNotPendingError):
        roll_vt_cue(cue.id)


# ---- 3. op_roll_vt エンドポイント ----


@pytest.mark.django_db
def test_op_roll_vt_happy_path(staff_client, channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/roll-vt/"))
    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    assert cue.fired_event_id is not None
    assert PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.PLAY_VT).exists()


@pytest.mark.django_db
def test_op_roll_vt_409_when_not_pending(staff_client, channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset, state=LiveCueState.AIRED)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/roll-vt/"))
    assert res.status_code == 409


@pytest.mark.django_db
def test_op_roll_vt_404_for_other_channel_cue(staff_client, channel):
    other = Channel.objects.create(name="別ch", slug="ch-other-vt", enabled=True, agent_token="t2")
    asset = _vt_asset()
    cue = _vt_cue(other, asset)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/roll-vt/"))
    assert res.status_code == 404


@pytest.mark.django_db
def test_op_roll_vt_requires_staff(http_client, channel):
    asset = _vt_asset()
    cue = _vt_cue(channel, asset)
    res = http_client.post(_url(channel.slug, f"cue/{cue.id}/roll-vt/"))
    assert res.status_code == 302


# ---- 4. sales/billing 非該当 (§6.9 の確認・変更しないことの回帰テスト) ----


@pytest.mark.django_db
def test_play_vt_excluded_from_sales_air_actions():
    """VT roll は放確 (sales) の対象外であること — 追加してはいけない (課金/放確誤検知を防ぐ)。"""
    from sales.tasks import _AIR_ACTIONS

    assert PlayoutAction.PLAY_VT not in _AIR_ACTIONS


@pytest.mark.django_db
def test_play_vt_excluded_from_billing_precheck_actions():
    """billing.services.precheck() の対象 action にも PLAY_VT を含めないこと
    (ソース中の action__in タプルに PLAY_VT の文字列が出現しないことを確認)。"""
    import inspect

    from billing import services as billing_services

    src = inspect.getsource(billing_services.precheck)
    assert "PlayoutAction.PLAY_VT" not in src


# ---- 5. auto_fire 予約イベントのキャンセル (タイムキープ Phase2 §2/D3・二重発火防止) ----


@pytest.mark.django_db
def test_roll_vt_cue_cancels_scheduled_auto_fire_event(channel):
    """VT cue の auto_fire は PLAY_VT 1件のみ予約される (CM と違いバンパー無し)。手動
    roll_vt_cue はこれを即座に CANCELLED にし、新規発火イベントを cue に紐づけること。"""
    asset = _vt_asset()
    cue, prog = _vt_auto_fire_cue(channel, asset, auto_offset_ms=120_000)
    _resolve_program_window(prog)

    auto_fire_events = list(
        PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    )
    assert len(auto_fire_events) == 1
    assert auto_fire_events[0].action == PlayoutAction.PLAY_VT
    assert auto_fire_events[0].status == PlayoutStatus.SCHEDULED
    auto_fire_id = auto_fire_events[0].id

    result = roll_vt_cue(cue.id)

    auto_fire_events[0].refresh_from_db()
    assert auto_fire_events[0].status == PlayoutStatus.CANCELLED
    assert result.state == LiveCueState.AIRED
    assert result.fired_event_id is not None
    assert result.fired_event_id != auto_fire_id
    assert result.fired_event.status == PlayoutStatus.SCHEDULED
