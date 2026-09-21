# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""emit_live の auto_fire 予約発行 (タイムキープ Phase2 §1・#25、resolver.py 拡張)。

Phase1 では LiveCue.auto_fire/auto_offset_ms は保持されるだけで一切実行されなかった。
Phase2 Batch A は resolver.emit_live がこれを実際に PendingEvent として予約発行するようにする。

最重要なのは D1 (`state=LiveCueState.PENDING` フィルタ) が二重発火を防ぐことの回帰テスト:
- auto_fire=True でも state が aired/skipped の cue は無視すること (test_..._ignores_non_pending_state)
- 一度 resolve() で予約発行した後、cue が (Batch B の手動発火/スキップ相当で) pending 以外に
  変わったら、次の resolve() で予約済みイベントが CANCELLED になり、それ以降も復活しないこと
  (test_..._event_cancelled_after_cue_leaves_pending_state)
- 同じ状態のまま resolve() を繰り返しても同一行が生存し続けること (再作成/キャンセルされない)
  (test_..._survives_re_resolve)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from django.utils import timezone

from core.models import LiveSource
from medialib.models import (
    Asset,
    AssetKind,
    CmBundle,
    CmBundleItem,
    CmCreative,
    CmGrid,
    NormalizeStatus,
)
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import LiveCue, LiveCueKind, LiveCueState, LiveRundown, Program, ProgramType
from scheduling.resolver import _commit, emit_live, resolve

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


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


def _cm_bundle(name="bundleA", durations=(15_000, 20_000)):
    bundle = CmBundle.objects.create(name=name)
    for i, dur in enumerate(durations):
        a = Asset.objects.create(
            kind=AssetKind.CM,
            title=f"{name}-cm{i}",
            duration_ms=dur,
            r2_key=f"mezzanine/cm/{name}-{i}.mp4",
            normalize_status=NormalizeStatus.READY,
        )
        cr = CmCreative.objects.create(asset=a, advertiser="adv", grid=CmGrid.G15)
        CmBundleItem.objects.create(cm_bundle=bundle, seq=i, cm_asset=cr)
    return bundle


def _vt_asset(title="VTR1", duration_ms=90_000, r2_key="mezzanine/vt/vtr1.mp4"):
    return Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title=title,
        duration_ms=duration_ms,
        r2_key=r2_key,
        normalize_status=NormalizeStatus.READY,
    )


def _cm_cue(
    rundown,
    bundle,
    *,
    seq=1,
    auto_fire=True,
    auto_offset_ms=None,
    state=LiveCueState.PENDING,
):
    return LiveCue.objects.create(
        rundown=rundown,
        seq=seq,
        kind=LiveCueKind.CM,
        planned_duration_ms=30_000,
        cm_bundle=bundle,
        grid="15s",
        state=state,
        auto_fire=auto_fire,
        auto_offset_ms=auto_offset_ms,
    )


def _vt_cue(
    rundown,
    asset,
    *,
    seq=1,
    auto_fire=True,
    auto_offset_ms=None,
    state=LiveCueState.PENDING,
):
    return LiveCue.objects.create(
        rundown=rundown,
        seq=seq,
        kind=LiveCueKind.VT,
        label=asset.title,
        planned_duration_ms=asset.duration_ms,
        asset=asset,
        state=state,
        auto_fire=auto_fire,
        auto_offset_ms=auto_offset_ms,
    )


# ---- 1. CM cue: 本体(PLAY_CM_BUNDLE) + layer20 バンパー show/hide の3件 ----


def test_emit_live_cm_auto_fire_emits_bundle_and_bumpers(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000, 20_000))
    cue = _cm_cue(rundown, bundle, auto_offset_ms=600_000)  # 開始10分後

    events = emit_live(prog)

    assert len(events) == 4
    cut_live = next(e for e in events if e.action == PlayoutAction.CUT_LIVE)
    play = next(e for e in events if e.action == PlayoutAction.PLAY_CM_BUNDLE)
    show = next(
        e
        for e in events
        if e.action == PlayoutAction.OVERLAY_OP and e.params["overlay_op"] == "show"
    )
    hide = next(
        e
        for e in events
        if e.action == PlayoutAction.OVERLAY_OP and e.params["overlay_op"] == "hide"
    )

    at = prog.start_at + timedelta(milliseconds=600_000)
    assert play.scheduled_at == at
    assert show.scheduled_at == at
    assert hide.scheduled_at == at + timedelta(milliseconds=35_000)  # 15000+20000

    # params 形状
    assert play.cm_bundle_id == bundle.id
    assert play.program_id == prog.id
    assert play.params["live_cue_id"] == cue.id
    assert play.params["bundle_id"] == bundle.id
    assert play.params["clips"] == f"cm/{bundle.items.all()[0].cm_asset_id}:15000," + (
        f"cm/{bundle.items.all()[1].cm_asset_id}:20000"
    )
    # cg_* は CUT_LIVE の params からそのままコピーされること
    assert play.params["cg_title"] == cut_live.params["cg_title"] == prog.title
    assert play.params["cg_channel"] == cut_live.params["cg_channel"] == channel.name
    # 生ソースありなので return_rtmp_app/key も転写される
    assert play.params["return_rtmp_app"] == "live"
    assert play.params["return_rtmp_key"] == "k"

    assert show.params == {
        "overlay_layer": "20",
        "overlay_op": "show",
        "overlay_kind": "graphic",
        "overlay_template": "bumper/cm-in",
        "overlay_data": "{}",
        "live_cue_id": cue.id,
    }
    assert hide.params == {
        "overlay_layer": "20",
        "overlay_op": "hide",
        "overlay_kind": "graphic",
        "live_cue_id": cue.id,
    }


def test_emit_live_cm_auto_fire_defaults_offset_zero(channel):
    """auto_offset_ms=None (未設定) は 0 扱い = 番組開始と同時刻。"""
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000,))
    _cm_cue(rundown, bundle, auto_offset_ms=None)

    events = emit_live(prog)
    play = next(e for e in events if e.action == PlayoutAction.PLAY_CM_BUNDLE)
    assert play.scheduled_at == prog.start_at


# ---- 2. VT cue: PLAY_VT の1件 ----


def test_emit_live_vt_auto_fire_emits_play_vt(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    asset = _vt_asset(duration_ms=90_000, r2_key="mezzanine/vt/vtr1.mp4")
    cue = _vt_cue(rundown, asset, auto_offset_ms=120_000)

    events = emit_live(prog)

    assert len(events) == 2
    cut_live = next(e for e in events if e.action == PlayoutAction.CUT_LIVE)
    vt = next(e for e in events if e.action == PlayoutAction.PLAY_VT)

    assert vt.scheduled_at == prog.start_at + timedelta(milliseconds=120_000)
    assert vt.asset_id == asset.id
    assert vt.program_id == prog.id
    assert vt.params["live_cue_id"] == cue.id
    assert vt.params["clip"] == f"asset/{asset.id}"
    assert vt.params["in_ms"] == 0
    assert vt.params["out_ms"] == 90_000
    assert vt.params["r2_key"] == "mezzanine/vt/vtr1.mp4"
    assert vt.params["cg_title"] == cut_live.params["cg_title"] == prog.title
    assert vt.params["return_rtmp_app"] == "live"
    assert vt.params["return_rtmp_key"] == "k"


def test_emit_live_vt_auto_fire_omits_r2_key_when_absent(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    asset = _vt_asset(r2_key="")
    _vt_cue(rundown, asset)

    events = emit_live(prog)
    vt = next(e for e in events if e.action == PlayoutAction.PLAY_VT)
    assert "r2_key" not in vt.params


# ---- 3. auto_fire=False → 追加イベントなし ----


def test_emit_live_ignores_auto_fire_false(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle()
    _cm_cue(rundown, bundle, auto_fire=False)

    events = emit_live(prog)
    assert len(events) == 1
    assert events[0].action == PlayoutAction.CUT_LIVE


# ---- 4. D1 (最重要): state != pending は無視 ----


def test_emit_live_ignores_cue_already_aired(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle()
    _cm_cue(rundown, bundle, state=LiveCueState.AIRED)

    events = emit_live(prog)
    assert len(events) == 1
    assert events[0].action == PlayoutAction.CUT_LIVE


def test_emit_live_ignores_cue_skipped(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    asset = _vt_asset()
    _vt_cue(rundown, asset, state=LiveCueState.SKIPPED)

    events = emit_live(prog)
    assert len(events) == 1
    assert events[0].action == PlayoutAction.CUT_LIVE


def test_emit_live_ignores_cue_firing(channel):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle()
    _cm_cue(rundown, bundle, state=LiveCueState.FIRING)

    events = emit_live(prog)
    assert len(events) == 1


def test_emit_live_no_rundown_at_all(channel):
    """rundown 自体が存在しない番組でも従来どおり CUT_LIVE 1件のみ (既存 idiom の安全性)。"""
    prog = _live_program(channel)
    events = emit_live(prog)
    assert len(events) == 1
    assert events[0].action == PlayoutAction.CUT_LIVE


# ---- 5. 空の CM バンドル → クラッシュせずスキップ ----


def test_emit_live_skips_empty_cm_bundle_without_crash(channel, caplog):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    empty_bundle = CmBundle.objects.create(name="empty")
    _cm_cue(rundown, empty_bundle, auto_offset_ms=1_000)

    with caplog.at_level(logging.WARNING, logger="scheduling.resolver"):
        events = emit_live(prog)

    assert len(events) == 1
    assert events[0].action == PlayoutAction.CUT_LIVE
    assert any("空" in r.getMessage() for r in caplog.records)


def test_emit_live_skips_cue_with_overflowing_auto_offset_ms_without_crash(channel, caplog):
    """auto_offset_ms は edit_views 側で境界検証されるが (test_live_rundown.py 参照)、
    既存データや検証を経ない経路からの異常値でチャンネル丸ごとの resolve が止まらないよう、
    resolver 側にも防御を入れている (2026-07-04 レビューで実証: 10**18ms のような値は
    timedelta 変換で OverflowError を起こし、この防御が無いと resolve_window/
    resolve_channel_now が例外で止まっていた)。"""
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000,))
    _cm_cue(rundown, bundle, auto_offset_ms=10**18)

    with caplog.at_level(logging.WARNING, logger="scheduling.resolver"):
        events = emit_live(prog)  # 例外を投げず、この cue だけスキップすること

    assert len(events) == 1
    assert events[0].action == PlayoutAction.CUT_LIVE
    assert any("不正" in r.getMessage() for r in caplog.records)


# ---- 6. 再解決での生存確認 (最重要: idempotency_key ベースの生存機構) ----


def test_emit_live_cm_auto_fire_survives_re_resolve(channel):
    now = timezone.now()
    prog = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
    )
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(rundown, bundle, auto_offset_ms=5_000)

    t0, t1 = now, now + timedelta(hours=2)
    resolve(channel, t0, t1)

    play1 = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_CM_BUNDLE, params__live_cue_id=cue.id
    )
    assert play1.status == PlayoutStatus.SCHEDULED
    pk1, key1 = play1.pk, play1.idempotency_key

    stats2 = resolve(channel, t0, t1)

    play2 = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_CM_BUNDLE, params__live_cue_id=cue.id
    )
    assert play2.pk == pk1  # 同一行 (再作成されていない)
    assert play2.idempotency_key == key1
    assert play2.status == PlayoutStatus.SCHEDULED
    assert (
        PlayoutEvent.objects.filter(
            channel=channel, action=PlayoutAction.PLAY_CM_BUNDLE, params__live_cue_id=cue.id
        ).count()
        == 1
    )
    # このイベント (バンパー2件含め計3件) はどれも cancel されない
    assert stats2["cancelled"] == 0


def test_emit_live_vt_auto_fire_survives_re_resolve(channel):
    now = timezone.now()
    prog = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
    )
    rundown = LiveRundown.objects.create(program=prog)
    asset = _vt_asset()
    cue = _vt_cue(rundown, asset, auto_offset_ms=10_000)

    t0, t1 = now, now + timedelta(hours=2)
    resolve(channel, t0, t1)
    vt1 = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_VT, params__live_cue_id=cue.id
    )
    pk1 = vt1.pk

    resolve(channel, t0, t1)
    vt2 = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_VT, params__live_cue_id=cue.id
    )
    assert vt2.pk == pk1
    assert vt2.status == PlayoutStatus.SCHEDULED


# ---- 7. D1 の帰結: pending を抜けた cue の予約済みイベントは次の resolve で CANCELLED ----


def test_emit_live_cm_auto_fire_event_cancelled_after_cue_leaves_pending_state(channel):
    """Batch B (services.py 手動発火/スキップ) がいずれ行う状態遷移を、その実装無しにここで
    直接シミュレートする (cue.state を pending から外すだけ)。resolve() が自然にこの cue を
    pending フィルタから除外し、_commit の cancel 分岐が予約済みイベントを CANCELLED に
    落とすことを確認する (D1 の帰結。手動系の即時キャンセル (D3) は Batch B の役割で、
    ここで確認するのは「最大5分後の次回 resolve でも安全側に倒れる」という保険側の性質)。
    """
    now = timezone.now()
    prog = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
    )
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(rundown, bundle, auto_offset_ms=5_000)

    t0, t1 = now, now + timedelta(hours=2)
    resolve(channel, t0, t1)

    linked = PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    assert linked.count() == 3  # PLAY_CM_BUNDLE + show + hide
    assert all(e.status == PlayoutStatus.SCHEDULED for e in linked)

    cue.state = LiveCueState.AIRED
    cue.save(update_fields=["state"])

    resolve(channel, t0, t1)

    linked2 = PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    assert linked2.count() == 3  # tombstone として行は残る
    assert all(e.status == PlayoutStatus.CANCELLED for e in linked2)

    # 復活しないこと (D1 が効き続けている)
    resolve(channel, t0, t1)
    linked3 = PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    assert all(e.status == PlayoutStatus.CANCELLED for e in linked3)


def test_emit_live_vt_auto_fire_event_cancelled_after_cue_skipped(channel):
    now = timezone.now()
    prog = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
    )
    rundown = LiveRundown.objects.create(program=prog)
    asset = _vt_asset()
    cue = _vt_cue(rundown, asset, auto_offset_ms=10_000)

    t0, t1 = now, now + timedelta(hours=2)
    resolve(channel, t0, t1)
    assert PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.PLAY_VT,
        params__live_cue_id=cue.id,
        status=PlayoutStatus.SCHEDULED,
    ).exists()

    cue.state = LiveCueState.SKIPPED
    cue.save(update_fields=["state"])
    resolve(channel, t0, t1)

    vt = PlayoutEvent.objects.get(
        channel=channel, action=PlayoutAction.PLAY_VT, params__live_cue_id=cue.id
    )
    assert vt.status == PlayoutStatus.CANCELLED


# ---- 8. resolve() の select_related("live_rundown") が正しく効くこと (N+1 回避) ----


def test_resolve_select_related_live_rundown_does_not_break_auto_fire(channel):
    """reverse OneToOne は select_related で辿れるはず (Django) だが、実装時点でこの資料が
    正しいことを実地確認する回帰テスト。rundown 有り/無し両方の番組が同一 resolve() window に
    混在しても正しく解決されること。"""
    now = timezone.now()
    prog_with_rundown = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
        title="rundownあり",
    )
    rundown = LiveRundown.objects.create(program=prog_with_rundown)
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(rundown, bundle, auto_offset_ms=1_000)

    prog_without_rundown = _live_program(
        channel,
        start=now + timedelta(hours=1, minutes=2),
        end=now + timedelta(hours=2, minutes=1),
        title="rundownなし",
    )

    resolve(channel, now, now + timedelta(hours=3))

    assert PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.PLAY_CM_BUNDLE, params__live_cue_id=cue.id
    ).exists()
    assert PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.CUT_LIVE, program=prog_without_rundown
    ).exists()


# ---- 9. _commit の TOCTOU レース修正 (2026-07-04 コードレビューで発見・実証) ----


def test_commit_does_not_revive_event_for_cue_that_left_pending_mid_resolve(channel):
    """resolve() は pending リストを非ロックで構築してから _commit() で確定する2段構成。
    この間に手動発火/スキップ (Batch B) が割り込み cue が PENDING を外れると、_commit() が
    古いスナップショットのまま「手動キャンセル直後の予約」を復活させ実際に二重発火しうる
    (このテストが再現する具体的なシナリオ)。修正: _commit() 冒頭で live_cue_id 付き
    PendingEvent を select_for_update により再確認し、PENDING を外れた cue の分は
    pending から除外してから確定させる。"""
    now = timezone.now()
    prog = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
    )
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(rundown, bundle, auto_offset_ms=5_000)

    t0, t1 = now, now + timedelta(hours=2)

    # resolver が「pending 構築」フェーズで cue を PENDING として読んだ古いスナップショット。
    stale_pending = emit_live(prog)
    assert any(pe.params.get("live_cue_id") == cue.id for pe in stale_pending), (
        "テスト前提: stale_pending に auto_fire イベントが含まれること"
    )

    # ここで手動発火 (Batch B) が割り込んで完了したと仮定 (cue が PENDING を外れる)。
    cue.state = LiveCueState.AIRED
    cue.save(update_fields=["state"])

    # resolver が (古いスナップショットのまま) 確定フェーズへ進む。
    stats = _commit(channel, t0, t1, stale_pending)

    linked = PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    assert linked.count() == 0, (
        "修正前は revived=3 (または created=3) となり、手動キャンセル直後の"
        "auto_fire イベントが復活/新規作成されて二重発火しうる不具合があった"
    )
    assert stats["revived"] == 0
    # created=1 は CUT_LIVE (プログラム自体の本線イベント、cue の auto_fire とは無関係で
    # 毎回作成される)。auto_fire 由来の3件 (本体+バンパー show/hide) が created/revived の
    # どちらにも含まれていないことが本テストの核心。
    assert stats["created"] == 1


def test_commit_still_creates_events_when_cue_remains_pending(channel):
    """通常経路 (レースが起きないケース) の回帰確認: cue が PENDING のままなら、
    live_cue_id の再確認を追加しても従来通り作成されること。"""
    now = timezone.now()
    prog = _live_program(
        channel,
        start=now + timedelta(minutes=1),
        end=now + timedelta(minutes=1, hours=1),
    )
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(rundown, bundle, auto_offset_ms=5_000)

    t0, t1 = now, now + timedelta(hours=2)
    pending = emit_live(prog)
    stats = _commit(channel, t0, t1, pending)

    assert stats["created"] == 4  # CUT_LIVE + PLAY_CM_BUNDLE + バンパー show/hide
    linked = PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    assert linked.count() == 3
    assert all(e.status == PlayoutStatus.SCHEDULED for e in linked)


def test_commit_race_fix_does_not_affect_events_without_live_cue_id(channel):
    """live_cue_id を持たない通常の PendingEvent (録画番組の CUT_LIVE 等) は、
    レース対策の追加フィルタの影響を受けないこと。"""
    now = timezone.now()
    prog = _live_program(channel, start=now + timedelta(minutes=1))
    t0, t1 = now, now + timedelta(hours=2)
    pending = emit_live(prog)
    assert not any(pe.params.get("live_cue_id") for pe in pending)

    stats = _commit(channel, t0, t1, pending)
    assert stats["created"] == 1  # CUT_LIVE のみ
    assert PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.CUT_LIVE, program=prog
    ).exists()


# ---- D1: 壁時計アンカー (auto_anchor=wallclock) ----


def test_emit_live_cm_auto_fire_wallclock_anchor(channel):
    """壁時計アンカーは番組日の固定時刻 (チャンネル TZ=Asia/Tokyo) に発火し、開始相対 offset を
    無視する。番組開始 21:00 JST でも auto_wall_time=22:00 の cue は 22:00 JST に予約される。"""
    from datetime import time

    from scheduling.models import LiveCueAnchor

    prog = _live_program(channel, start=BASE)  # 12:00 UTC = 21:00 JST 開始
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle()
    cue = _cm_cue(rundown, bundle, auto_offset_ms=600_000)  # 開始相対 offset は無視されるべき
    cue.auto_anchor = LiveCueAnchor.WALLCLOCK
    cue.auto_wall_time = time(22, 0)  # 22:00 JST (開始 21:00 JST より後 → 当日側)
    cue.save()

    events = emit_live(prog)
    play = next(e for e in events if e.action == PlayoutAction.PLAY_CM_BUNDLE)
    # 開始相対 (21:10 JST) ではなく壁時計 22:00 JST に発火 = ドリフト非依存。
    assert play.scheduled_at != prog.start_at + timedelta(milliseconds=600_000)
    assert timezone.localtime(play.scheduled_at).strftime("%H:%M") == "22:00"


def test_emit_live_cm_auto_fire_wallclock_without_time_falls_back_to_offset(channel):
    """auto_anchor=wallclock でも auto_wall_time 未設定なら開始相対 offset にフォールバック
    (防御・_auto_fire_at のガード)。"""
    from scheduling.models import LiveCueAnchor

    prog = _live_program(channel, start=BASE)
    rundown = LiveRundown.objects.create(program=prog)
    bundle = _cm_bundle()
    cue = _cm_cue(rundown, bundle, auto_offset_ms=600_000)
    cue.auto_anchor = LiveCueAnchor.WALLCLOCK
    cue.auto_wall_time = None
    cue.save()
    events = emit_live(prog)
    play = next(e for e in events if e.action == PlayoutAction.PLAY_CM_BUNDLE)
    assert play.scheduled_at == prog.start_at + timedelta(milliseconds=600_000)
