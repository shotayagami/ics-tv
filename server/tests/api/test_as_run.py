# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""as-run 受信 (playout.grpc_service.apply_result) の単体テスト。

gRPC 経由ではなく increment ロジックを直接検証する (smoke は test_grpc_smoke.py)。
焦点: PLAY_CM の DONE 確定で CmCreative.aired_count が増え、再送では二重計上しないこと。
併せて、タイムキープ Phase2 §3/D5 (auto_fire 予約イベントが実際に DONE になったら対応する
LiveCue を aired 化する反映ロジック) も検証する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from core.models import LiveSource
from medialib.models import Asset, AssetKind, CmCreative, CmGrid, NormalizeStatus
from playout.grpc_service import apply_result
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import LiveCue, LiveCueKind, LiveCueState, LiveRundown, Program, ProgramType

pytestmark = pytest.mark.django_db

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _cm_creative(aired: int = 0, max_airings: int | None = None) -> CmCreative:
    asset = Asset.objects.create(
        kind=AssetKind.CM,
        title="CM acme 15s",
        duration_ms=15000,
        r2_key="mezzanine/cm/1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(
        asset=asset,
        advertiser="acme",
        grid=CmGrid.G15,
        aired_count=aired,
        max_airings=max_airings,
    )


def _event(
    channel, action, *, asset=None, status=PlayoutStatus.SCHEDULED, params=None
) -> PlayoutEvent:
    return PlayoutEvent.objects.create(
        idempotency_key=uuid.uuid4(),
        channel=channel,
        scheduled_at=timezone.now(),
        action=action,
        asset=asset,
        status=status,
        params=params or {},
    )


def _live_program(channel, start=BASE, title="生番組"):
    ls = LiveSource.objects.create(name="OBS", rtmp_app="live", rtmp_key="k")
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=start + timedelta(hours=1),
        live_source=ls,
    )


def _live_cue(channel, kind, *, state=LiveCueState.PENDING, fired_event=None) -> LiveCue:
    """auto_fire=True の LiveCue (タイムキープ Phase2 §3 反映テスト用。cm_bundle/asset は
    apply_result 側のロジックが参照しないため未設定のままでよい)。"""
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=kind,
        planned_duration_ms=30_000,
        state=state,
        fired_event=fired_event,
        auto_fire=True,
    )


def test_play_cm_done_increments_aired_count(channel):
    cm = _cm_creative(aired=0)
    ev = _event(channel, PlayoutAction.PLAY_CM, asset=cm.asset)

    ok = apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    assert ok is True
    ev.refresh_from_db()
    cm.refresh_from_db()
    assert ev.status == PlayoutStatus.DONE
    assert cm.aired_count == 1


def test_repeated_done_does_not_double_count(channel):
    """agent の outbox 再送で同じ key が複数回届いても 1 回だけ増分。"""
    cm = _cm_creative(aired=0)
    ev = _event(channel, PlayoutAction.PLAY_CM, asset=cm.asset)
    key = str(ev.idempotency_key)

    apply_result(key, PlayoutStatus.DONE, timezone.now(), "")
    apply_result(key, PlayoutStatus.DONE, timezone.now(), "resend")

    cm.refresh_from_db()
    assert cm.aired_count == 1


def test_failed_status_does_not_increment(channel):
    cm = _cm_creative(aired=0)
    ev = _event(channel, PlayoutAction.PLAY_CM, asset=cm.asset)

    apply_result(str(ev.idempotency_key), PlayoutStatus.FAILED, timezone.now(), "boom")

    cm.refresh_from_db()
    assert cm.aired_count == 0


def test_non_cm_action_does_not_touch_creatives(channel, asset_ready):
    """PLAY_ASSET (本編) の DONE は CM の aired_count に影響しない。"""
    cm = _cm_creative(aired=3)
    ev = _event(channel, PlayoutAction.PLAY_ASSET, asset=asset_ready)

    ok = apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    assert ok is True
    cm.refresh_from_db()
    assert cm.aired_count == 3


def test_unknown_key_returns_false(channel):
    ok = apply_result(str(uuid.uuid4()), PlayoutStatus.DONE, timezone.now(), "")
    assert ok is False


# ---- タイムキープ Phase2 §3/D5: auto_fire LiveCue の aired 化 ----


def test_apply_result_marks_cm_live_cue_aired_on_first_done(channel):
    """params["live_cue_id"] 付きの PLAY_CM_BUNDLE が初回 DONE になったら、対応する LiveCue
    (state=PENDING) が AIRED 化され fired_event がその event を指すこと。"""
    cue = _live_cue(channel, LiveCueKind.CM)
    ev = _event(channel, PlayoutAction.PLAY_CM_BUNDLE, params={"live_cue_id": cue.id})

    ok = apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    assert ok is True
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    assert cue.fired_event_id == ev.id


def test_apply_result_marks_vt_live_cue_aired_on_first_done(channel):
    """PLAY_VT でも同様に aired 化されること。"""
    cue = _live_cue(channel, LiveCueKind.VT)
    ev = _event(channel, PlayoutAction.PLAY_VT, params={"live_cue_id": cue.id})

    apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    assert cue.fired_event_id == ev.id


def test_apply_result_does_not_clobber_already_aired_live_cue(channel):
    """D5 のガード: cue が既に(手動発火などで)AIRED+別の fired_event を持つ場合、たまたま
    同じ live_cue_id を params に持つ別の(古い/競合した)イベントが後から DONE 化されても
    上書きしないこと (state=PENDING フィルタが効いていることの回帰テスト)。"""
    manual_ev = _event(channel, PlayoutAction.PLAY_CM_BUNDLE, status=PlayoutStatus.DONE)
    cue = _live_cue(channel, LiveCueKind.CM, state=LiveCueState.AIRED, fired_event=manual_ev)
    stale_ev = _event(channel, PlayoutAction.PLAY_CM_BUNDLE, params={"live_cue_id": cue.id})

    ok = apply_result(str(stale_ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    assert ok is True
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    assert cue.fired_event_id == manual_ev.id  # 上書きされていない (stale_ev ではない)


def test_apply_result_does_not_touch_skipped_live_cue(channel):
    """SKIPPED な cue も同様に state=PENDING フィルタ対象外で保護されること。"""
    cue = _live_cue(channel, LiveCueKind.VT, state=LiveCueState.SKIPPED)
    ev = _event(channel, PlayoutAction.PLAY_VT, params={"live_cue_id": cue.id})

    apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    cue.refresh_from_db()
    assert cue.state == LiveCueState.SKIPPED
    assert cue.fired_event_id is None


def test_apply_result_ignores_overlay_op_bumper_for_live_cue(channel):
    """バンパー(OVERLAY_OP show/hide)も同じ live_cue_id を params に持つが、cue の aired 化を
    担うのは本体イベントのみ (ev.action の絞り込みが効いていること)。"""
    cue = _live_cue(channel, LiveCueKind.CM)
    bumper_ev = _event(
        channel,
        PlayoutAction.OVERLAY_OP,
        params={"live_cue_id": cue.id, "overlay_op": "show", "overlay_layer": "20"},
    )

    ok = apply_result(str(bumper_ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")

    assert ok is True
    cue.refresh_from_db()
    assert cue.state == LiveCueState.PENDING
    assert cue.fired_event_id is None


def test_apply_result_does_not_resurrect_cancelled_event(channel):
    """2026-07-04 レビュー指摘: apply_result はこれまで ev.status を無条件に上書きしていたため、
    手動キャンセル (tombstone) 済みのイベントに agent の遅延/再送 as-run 報告が届くと
    CANCELLED→DONE へ復活してしまい、二重発火の実害を隠したまま「正常完了」に見せかける
    恐れがあった。CANCELLED は終端状態として扱い、以後の状態遷移を一切適用しない。"""
    cue = _live_cue(channel, LiveCueKind.CM)
    ev = _event(
        channel,
        PlayoutAction.PLAY_CM_BUNDLE,
        status=PlayoutStatus.CANCELLED,
        params={"live_cue_id": cue.id},
    )

    ok = apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "stale report")

    assert ok is True  # 受信自体は accepted 扱い (ReportResult の "not found" 警告と区別)
    ev.refresh_from_db()
    assert ev.status == PlayoutStatus.CANCELLED  # DONE へ上書きされていない
    assert ev.note is None  # note/actual_at も更新されていない (フィールド既定のまま)
    cue.refresh_from_db()
    assert cue.state == LiveCueState.PENDING  # LiveCue も aired 化されない
    assert cue.fired_event_id is None


def test_apply_result_without_live_cue_id_does_not_error(channel):
    """live_cue_id を持たない通常イベント (手動発火や通常番組送出) の DONE 化はこれまでどおり
    何の副作用も起こさないこと (回帰なしの確認)。"""
    ev = _event(channel, PlayoutAction.PLAY_CM_BUNDLE, params={"bundle_id": 1})
    ok = apply_result(str(ev.idempotency_key), PlayoutStatus.DONE, timezone.now(), "")
    assert ok is True  # 例外を投げず正常終了する


def test_repeated_done_does_not_reprocess_live_cue(channel):
    """outbox 再送で同じ idempotency_key が複数回届いても、2回目以降は first_done=False な
    ので LiveCue 反映ロジックが再実行されず、fired_event が変わらないこと。"""
    cue = _live_cue(channel, LiveCueKind.CM)
    ev = _event(channel, PlayoutAction.PLAY_CM_BUNDLE, params={"live_cue_id": cue.id})
    key = str(ev.idempotency_key)

    apply_result(key, PlayoutStatus.DONE, timezone.now(), "")
    cue.refresh_from_db()
    first_fired_event_id = cue.fired_event_id

    apply_result(key, PlayoutStatus.DONE, timezone.now(), "resend")
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    assert cue.fired_event_id == first_fired_event_id == ev.id
