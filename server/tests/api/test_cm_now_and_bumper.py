# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CM cue 発火 (fire_cm_now_cue/op_cm_now) + CG バンパー (layer20) + skip_live_cue のテスト
(タイムキープ Phase 1 §4/§5・#25)。

core.ops_views.fire_cm_bundle (op_cm_in と op_cm_now が共有する実装)・core.views.schedule_future_event
(fire_chime/fire_breaking_telop と共有する idiom) の新規抽出・追加を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from core.models import Channel, LiveSource
from core.ops_views import EmptyCmBundleError, fire_cm_bundle
from core.views import schedule_future_event
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
from scheduling.resolver import resolve
from scheduling.services import CueNotPendingError, fire_cm_now_cue, skip_live_cue

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
BUMPER_LAYER = "20"


def _url(slug: str, suffix: str) -> str:
    return f"/ops/ch/{slug}/{suffix}"


def _cm_bundle(name="bundleA", durations=(15_000, 20_000)):
    bundle = CmBundle.objects.create(name=name)
    for i, dur in enumerate(durations):
        a = Asset.objects.create(
            kind=AssetKind.CM,
            title=f"cm{i}",
            duration_ms=dur,
            r2_key=f"mezzanine/cm/{name}-{i}.mp4",
            normalize_status=NormalizeStatus.READY,
        )
        cr = CmCreative.objects.create(asset=a, advertiser="adv", grid=CmGrid.G15)
        CmBundleItem.objects.create(cm_bundle=bundle, seq=i, cm_asset=cr)
    return bundle


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


def _cm_cue(channel, bundle, state=LiveCueState.PENDING):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.CM,
        planned_duration_ms=sum(
            it.cm_asset.asset.duration_ms for it in bundle.items.select_related("cm_asset__asset")
        ),
        cm_bundle=bundle,
        grid="15s",
        state=state,
    )


def _cm_auto_fire_cue(channel, bundle, *, seq=1, rundown=None, auto_offset_ms=0, start=BASE):
    """auto_fire=True の CM cue を作る (タイムキープ Phase2 §2 のキャンセルテスト用)。

    呼び出し側が `resolve()` を実行して初めて予約 PlayoutEvent (本体+バンパー show/hide)
    が実発行される (Batch A の `_emit_live_auto_fire_cues`)。戻り値: (cue, prog)。
    """
    if rundown is None:
        prog = _live_program(channel, start=start)
        rundown = LiveRundown.objects.create(program=prog)
    else:
        prog = rundown.program
    cue = LiveCue.objects.create(
        rundown=rundown,
        seq=seq,
        kind=LiveCueKind.CM,
        planned_duration_ms=sum(
            it.cm_asset.asset.duration_ms for it in bundle.items.select_related("cm_asset__asset")
        ),
        cm_bundle=bundle,
        grid="15s",
        state=LiveCueState.PENDING,
        auto_fire=True,
        auto_offset_ms=auto_offset_ms,
    )
    return cue, prog


def _resolve_program_window(prog) -> None:
    resolve(prog.channel, prog.start_at - timedelta(minutes=1), prog.end_at + timedelta(minutes=1))


# ---- 1. fire_cm_bundle: PLAY_CM_BUNDLE + layer20 バンパー show/hide ----


@pytest.mark.django_db
def test_fire_cm_bundle_inserts_play_and_bumper_show_and_scheduled_hide(channel):
    bundle = _cm_bundle(durations=(15_000, 20_000))
    before = timezone.now()
    ev = fire_cm_bundle(channel, bundle, discriminator="disc1")
    after = timezone.now()

    assert ev.action == PlayoutAction.PLAY_CM_BUNDLE
    assert ev.cm_bundle_id == bundle.id
    assert ev.params["bundle_id"] == bundle.id
    assert ev.params["clips"].count(",") == 1
    assert ":15000" in ev.params["clips"] and ":20000" in ev.params["clips"]

    show = PlayoutEvent.objects.get(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="show",
    )
    assert show.params["overlay_kind"] == "graphic"
    assert show.params["overlay_template"] == "bumper/cm-in"

    hide = PlayoutEvent.objects.get(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
    )
    assert hide.status == PlayoutStatus.SCHEDULED
    # bundle_total_ms = 15000 + 20000 = 35000ms 後に hide が予定される
    expected_min = before + timedelta(milliseconds=35_000)
    expected_max = after + timedelta(milliseconds=35_000)
    assert expected_min <= hide.scheduled_at <= expected_max


@pytest.mark.django_db
def test_fire_cm_bundle_raises_for_empty_bundle(channel):
    empty = CmBundle.objects.create(name="empty")
    with pytest.raises(EmptyCmBundleError):
        fire_cm_bundle(channel, empty, discriminator="disc-empty")
    assert not PlayoutEvent.objects.filter(channel=channel).exists()


def _cut_live(channel, prog, params, scheduled_at=None):
    return PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=scheduled_at or prog.start_at,
        action=PlayoutAction.CUT_LIVE,
        status=PlayoutStatus.DONE,
        program=prog,
        params=params,
    )


@pytest.mark.django_db
def test_fire_cm_bundle_copies_return_rtmp_url_when_present(channel):
    prog = _live_program(channel)
    _cut_live(channel, prog, {"rtmp_url": "rtmp://ingest.example/live/ch1"})
    bundle = _cm_bundle()
    ev = fire_cm_bundle(channel, bundle, discriminator="disc-rtmp-url")
    assert ev.params["return_rtmp_url"] == "rtmp://ingest.example/live/ch1"


@pytest.mark.django_db
def test_fire_cm_bundle_falls_back_to_rtmp_app_key_when_no_rtmp_url(channel):
    """通常運用の emit_live (scheduling/resolver.py) は rtmp_url ではなく rtmp_app/rtmp_key
    しか params に持たない (agent 側で URL 組み立てさせる設計)。この場合も
    return_rtmp_app/return_rtmp_key を転写し、reel 末尾の自動本線復帰を機能させる
    (このフォールバックが無いと通常運用の CM 明けで自動復帰が一切発火しない)。"""
    prog = _live_program(channel)
    _cut_live(channel, prog, {"rtmp_app": "live", "rtmp_key": "ch1"})
    bundle = _cm_bundle()
    ev = fire_cm_bundle(channel, bundle, discriminator="disc-rtmp-fallback")
    assert ev.params["return_rtmp_app"] == "live"
    assert ev.params["return_rtmp_key"] == "ch1"
    assert "return_rtmp_url" not in ev.params


@pytest.mark.django_db
def test_fire_cm_bundle_without_prior_cut_live_has_no_return_rtmp(channel):
    bundle = _cm_bundle()
    ev = fire_cm_bundle(channel, bundle, discriminator="disc-no-cutlive")
    assert "return_rtmp_url" not in ev.params
    assert "return_rtmp_app" not in ev.params


# ---- 2. op_cm_in 経由でもバンパーが併発すること (既存 412/400/clips 挙動は test_ops_dashboard.py に委譲) ----


@pytest.mark.django_db
def test_op_cm_in_also_fires_bumper(staff_client, channel):
    bundle = _cm_bundle(durations=(15_000,))
    res = staff_client.post(_url(channel.slug, "cm-in/"), {"bundle_id": bundle.id})
    assert res.status_code == 200
    assert PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="show",
    ).exists()
    assert PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
        status=PlayoutStatus.SCHEDULED,
    ).exists()


@pytest.mark.django_db
def test_op_cm_in_twice_schedules_a_fresh_hide_each_time(staff_client, channel):
    """discriminator が固定文字列だと2回目の CM 入りが前回の (既に消化済みの) hide 行を
    get_or_create で再利用してしまい、新しい hide が一切スケジュールされなくなる回帰の防止。"""
    bundle = _cm_bundle(durations=(15_000,))
    res1 = staff_client.post(_url(channel.slug, "cm-in/"), {"bundle_id": bundle.id})
    assert res1.status_code == 200
    hide1 = PlayoutEvent.objects.get(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
    )
    # 1回目の hide を「既に agent が処理済み」の状態に模擬する。
    hide1.status = PlayoutStatus.DONE
    hide1.save(update_fields=["status"])

    res2 = staff_client.post(_url(channel.slug, "cm-in/"), {"bundle_id": bundle.id})
    assert res2.status_code == 200
    scheduled_hides = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
        status=PlayoutStatus.SCHEDULED,
    )
    # 2回目のCM入りで新しい (SCHEDULEDの) hide がちゃんと発行されること
    # (固定discriminatorのバグでは get_or_create が1回目のDONE行を返すだけで新規作成されない)。
    assert scheduled_hides.exists()
    assert not scheduled_hides.filter(pk=hide1.pk).exists()


# ---- 3. op_cm_return / op_cut_live_return: 予定済みバンパー hide を CANCELLED にして即時 hide ----


@pytest.mark.django_db
def test_op_cm_return_cancels_scheduled_bumper_and_fires_immediate_hide(staff_client, channel):
    bundle = _cm_bundle(durations=(15_000,))
    fire_cm_bundle(channel, bundle, discriminator="pre")
    old_hide = PlayoutEvent.objects.get(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
    )
    assert old_hide.status == PlayoutStatus.SCHEDULED

    res = staff_client.post(_url(channel.slug, "cm-return/"))
    assert res.status_code == 200

    old_hide.refresh_from_db()
    assert old_hide.status == PlayoutStatus.CANCELLED
    immediate_hides = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
    ).exclude(pk=old_hide.pk)
    assert immediate_hides.exists()
    assert immediate_hides.first().scheduled_at <= timezone.now()


@pytest.mark.django_db
def test_op_cut_live_return_cancels_scheduled_bumper_and_fires_immediate_hide(
    staff_client, channel
):
    bundle = _cm_bundle(durations=(15_000,))
    fire_cm_bundle(channel, bundle, discriminator="pre2")
    old_hide = PlayoutEvent.objects.get(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
    )

    res = staff_client.post(_url(channel.slug, "cut-live-return/"))
    assert res.status_code == 200

    old_hide.refresh_from_db()
    assert old_hide.status == PlayoutStatus.CANCELLED
    immediate_hides = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer=BUMPER_LAYER,
        params__overlay_op="hide",
    ).exclude(pk=old_hide.pk)
    assert immediate_hides.exists()


@pytest.mark.django_db
def test_op_cm_return_harmless_noop_without_bumper(staff_client, channel):
    """バンパー未表示 (CM未発火) でも cm-return は 200 のまま (no-op)。"""
    res = staff_client.post(_url(channel.slug, "cm-return/"))
    assert res.status_code == 200


# ---- 4. fire_cm_now_cue: 正常系 + 二重発火防止 ----


@pytest.mark.django_db
def test_fire_cm_now_cue_happy_path(channel):
    bundle = _cm_bundle(durations=(15_000, 20_000))
    cue = _cm_cue(channel, bundle)

    result = fire_cm_now_cue(cue.id)

    assert result.state == LiveCueState.AIRED
    assert result.fired_event_id is not None
    assert result.fired_event.action == PlayoutAction.PLAY_CM_BUNDLE
    assert (
        PlayoutEvent.objects.filter(
            channel=channel, action=PlayoutAction.OVERLAY_OP, params__overlay_layer=BUMPER_LAYER
        ).count()
        == 2
    )  # show + hide


@pytest.mark.django_db
def test_fire_cm_now_cue_double_fire_guard(channel):
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(channel, bundle)
    fire_cm_now_cue(cue.id)
    with pytest.raises(CueNotPendingError):
        fire_cm_now_cue(cue.id)


@pytest.mark.django_db
def test_fire_cm_now_cue_rejects_non_pending(channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle, state=LiveCueState.SKIPPED)
    with pytest.raises(CueNotPendingError):
        fire_cm_now_cue(cue.id)


@pytest.mark.django_db
def test_fire_cm_now_cue_propagates_empty_bundle_error(channel):
    empty = CmBundle.objects.create(name="empty")
    cue = _cm_cue(channel, empty)
    with pytest.raises(EmptyCmBundleError):
        fire_cm_now_cue(cue.id)


# ---- 5. skip_live_cue ----


@pytest.mark.django_db
def test_skip_live_cue_happy_path(channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle)
    result = skip_live_cue(cue.id)
    assert result.state == LiveCueState.SKIPPED
    assert not PlayoutEvent.objects.filter(channel=channel).exists()


@pytest.mark.django_db
def test_skip_live_cue_rejects_non_pending(channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle, state=LiveCueState.AIRED)
    with pytest.raises(CueNotPendingError):
        skip_live_cue(cue.id)


# ---- 6. op_cm_now / op_skip_cue エンドポイント ----


@pytest.mark.django_db
def test_op_cm_now_happy_path(staff_client, channel):
    bundle = _cm_bundle(durations=(15_000,))
    cue = _cm_cue(channel, bundle)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/cm-now/"))
    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED
    assert cue.fired_event_id is not None
    assert PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.PLAY_CM_BUNDLE
    ).exists()


@pytest.mark.django_db
def test_op_cm_now_409_when_not_pending(staff_client, channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle, state=LiveCueState.AIRED)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/cm-now/"))
    assert res.status_code == 409


@pytest.mark.django_db
def test_op_cm_now_412_for_empty_bundle(staff_client, channel):
    empty = CmBundle.objects.create(name="empty")
    cue = _cm_cue(channel, empty)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/cm-now/"))
    assert res.status_code == 412


@pytest.mark.django_db
def test_op_cm_now_404_for_other_channel_cue(staff_client, channel):
    other = Channel.objects.create(name="別ch", slug="ch-other", enabled=True, agent_token="t2")
    bundle = _cm_bundle()
    cue = _cm_cue(other, bundle)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/cm-now/"))
    assert res.status_code == 404


@pytest.mark.django_db
def test_op_skip_cue_happy_path(staff_client, channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/skip/"))
    assert res.status_code == 200
    cue.refresh_from_db()
    assert cue.state == LiveCueState.SKIPPED


@pytest.mark.django_db
def test_op_skip_cue_409_when_not_pending(staff_client, channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle, state=LiveCueState.SKIPPED)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/skip/"))
    assert res.status_code == 409


@pytest.mark.django_db
def test_op_skip_cue_404_for_other_channel_cue(staff_client, channel):
    other = Channel.objects.create(name="別ch2", slug="ch-other2", enabled=True, agent_token="t3")
    bundle = _cm_bundle()
    cue = _cm_cue(other, bundle)
    res = staff_client.post(_url(channel.slug, f"cue/{cue.id}/skip/"))
    assert res.status_code == 404


@pytest.mark.django_db
def test_ops_actions_require_staff(http_client, channel):
    bundle = _cm_bundle()
    cue = _cm_cue(channel, bundle)
    assert http_client.post(_url(channel.slug, f"cue/{cue.id}/cm-now/")).status_code == 302
    assert http_client.post(_url(channel.slug, f"cue/{cue.id}/skip/")).status_code == 302


# ---- 7. schedule_future_event: get_or_create 冪等性 ----


@pytest.mark.django_db
def test_schedule_future_event_idempotent_same_discriminator(channel):
    at = timezone.now() + timedelta(seconds=30)
    ev1 = schedule_future_event(
        channel, PlayoutAction.OVERLAY_OP, {"a": 1}, at, discriminator="dup-disc"
    )
    ev2 = schedule_future_event(
        channel, PlayoutAction.OVERLAY_OP, {"a": 1}, at, discriminator="dup-disc"
    )
    assert ev1.id == ev2.id
    assert (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.OVERLAY_OP).count() == 1
    )


@pytest.mark.django_db
def test_schedule_future_event_distinct_discriminator_creates_new_row(channel):
    at = timezone.now() + timedelta(seconds=30)
    schedule_future_event(channel, PlayoutAction.OVERLAY_OP, {"a": 1}, at, discriminator="disc-a")
    schedule_future_event(channel, PlayoutAction.OVERLAY_OP, {"a": 1}, at, discriminator="disc-b")
    assert (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.OVERLAY_OP).count() == 2
    )


# ---- 8. auto_fire 予約イベントのキャンセル (タイムキープ Phase2 §2/D3・二重発火防止) ----
#
# resolve() (Batch A の emit_live 拡張) が予約発行した auto_fire イベントを、手動発火/skip が
# 同一トランザクション内で即座に CANCELLED にすることを確認する。これが無いと最大5分後の
# 次回 resolve までの間、手動発火とauto_fire予約の両方が SCHEDULED のまま並存し二重発火しうる
# (放送事故に直結するため最重要のテスト群)。


@pytest.mark.django_db
def test_fire_cm_now_cue_cancels_scheduled_auto_fire_events(channel):
    """(a) auto_fire 予約済み3件(本体+バンパー show/hide)が全て CANCELLED になり、
    (b) 手動発火の新規イベントが別途存在し、(c) cue.state==AIRED で fired_event は
    その新規イベントを指す (CANCELLED になった auto_fire 側のどれでもない)こと。"""
    bundle = _cm_bundle(durations=(15_000,))
    cue, prog = _cm_auto_fire_cue(channel, bundle, auto_offset_ms=600_000)
    _resolve_program_window(prog)

    auto_fire_events = list(
        PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    )
    assert len(auto_fire_events) == 3  # PLAY_CM_BUNDLE + バンパー show + hide
    assert all(e.status == PlayoutStatus.SCHEDULED for e in auto_fire_events)
    auto_fire_ids = {e.id for e in auto_fire_events}

    result = fire_cm_now_cue(cue.id)

    # (a) 予約済み3件は全て CANCELLED
    for e in auto_fire_events:
        e.refresh_from_db()
        assert e.status == PlayoutStatus.CANCELLED
    # (b)+(c) 手動発火の新規イベントが cue に紐づき、CANCELLED 側とは別物
    assert result.state == LiveCueState.AIRED
    assert result.fired_event_id is not None
    assert result.fired_event_id not in auto_fire_ids
    assert result.fired_event.status == PlayoutStatus.SCHEDULED


@pytest.mark.django_db
def test_skip_live_cue_cancels_scheduled_auto_fire_events(channel):
    """auto_fire 予約済みの cue を skip すると、予約済みイベント(本体+バンパー2件)が全て
    CANCELLED になり cue.state==SKIPPED になること。"""
    bundle = _cm_bundle(durations=(15_000,))
    cue, prog = _cm_auto_fire_cue(channel, bundle, auto_offset_ms=600_000)
    _resolve_program_window(prog)

    auto_fire_events = list(
        PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue.id)
    )
    assert len(auto_fire_events) == 3
    assert all(e.status == PlayoutStatus.SCHEDULED for e in auto_fire_events)

    result = skip_live_cue(cue.id)

    assert result.state == LiveCueState.SKIPPED
    for e in auto_fire_events:
        e.refresh_from_db()
        assert e.status == PlayoutStatus.CANCELLED


@pytest.mark.django_db
def test_fire_cm_now_cue_cancellation_is_scoped_to_this_cue_only(channel):
    """フィルタの絞り込みが甘いと無関係な cue の auto_fire イベントまで巻き込んでキャンセル
    してしまう回帰を防ぐ: 同一チャンネル・別番組の2つの auto_fire cue のうち1つだけ手動発火し、
    もう片方の予約済みイベントが SCHEDULED のまま無傷であることを確認する。"""
    bundle_a = _cm_bundle(name="bundleA", durations=(15_000,))
    bundle_b = _cm_bundle(name="bundleB", durations=(20_000,))
    cue_a, prog_a = _cm_auto_fire_cue(channel, bundle_a, auto_offset_ms=60_000, start=BASE)
    cue_b, prog_b = _cm_auto_fire_cue(
        channel,
        bundle_b,
        auto_offset_ms=60_000,
        start=BASE
        + timedelta(hours=2),  # 別番組(同一チャンネル)。EXCLUDE制約回避のため時間帯を分離
    )
    # 別番組 (別rundown) なので resolve window をそれぞれ通す。
    _resolve_program_window(prog_a)
    _resolve_program_window(prog_b)

    events_b = list(PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue_b.id))
    assert len(events_b) == 3
    assert all(e.status == PlayoutStatus.SCHEDULED for e in events_b)

    fire_cm_now_cue(cue_a.id)

    # cue_a の auto_fire 予約は CANCELLED
    assert all(
        e.status == PlayoutStatus.CANCELLED
        for e in PlayoutEvent.objects.filter(channel=channel, params__live_cue_id=cue_a.id)
    )
    # cue_b は無傷 (SCHEDULED のまま)
    for e in events_b:
        e.refresh_from_db()
        assert e.status == PlayoutStatus.SCHEDULED
    cue_b.refresh_from_db()
    assert cue_b.state == LiveCueState.PENDING


# ---- D3: バンドル未割付 CM cue の grid 動的充填 (fire_cm_dynamic) ----


def _free_cm(advertiser="advX", dur_ms=15_000, grid=CmGrid.G15):
    a = Asset.objects.create(
        kind=AssetKind.CM,
        title=f"free-{advertiser}-{dur_ms}",
        duration_ms=dur_ms,
        r2_key=f"mezzanine/cm/free-{advertiser}-{dur_ms}.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    return CmCreative.objects.create(asset=a, advertiser=advertiser, grid=grid)


def _dyn_cm_cue(channel, *, grid="15s", target_ms=30_000):
    prog = _live_program(channel)
    rundown = LiveRundown.objects.create(program=prog)
    return LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.CM,
        planned_duration_ms=target_ms,
        cm_bundle=None,  # 未割付 → 発火時に grid 動的充填 (D3)
        grid=grid,
        state=LiveCueState.PENDING,
    )


def test_fire_cm_now_dynamic_fill(channel):
    """cm_bundle 未割付 CM cue は発火時に grid で在庫から動的充填し PLAY_CM_BUNDLE を出す。"""
    _free_cm("a", 15_000)
    _free_cm("b", 15_000)
    cue = _dyn_cm_cue(channel, grid="15s", target_ms=30_000)
    fire_cm_now_cue(cue.id)
    cue.refresh_from_db()
    assert cue.state == LiveCueState.AIRED and cue.fired_event_id is not None
    play = PlayoutEvent.objects.get(pk=cue.fired_event_id)
    assert play.action == PlayoutAction.PLAY_CM_BUNDLE
    assert play.cm_bundle_id is None  # 動的充填は bundle を持たない
    assert play.params["clips"].count("cm/") == 2  # 15s×2 で 30s 充填


def test_fire_cm_now_dynamic_fill_no_inventory_raises(channel):
    """在庫が無ければ EmptyCmBundleError (op_cm_now は 412 を返す)。"""
    cue = _dyn_cm_cue(channel, grid="15s", target_ms=30_000)
    with pytest.raises(EmptyCmBundleError):
        fire_cm_now_cue(cue.id)
    cue.refresh_from_db()
    assert cue.state == LiveCueState.PENDING  # 失敗時は pending のまま (atomic ロールバック)


# ---- D4: CM入りチャイム (ChimeCategory.CM_IN・fire_cm_bundle 併発) ----


def test_cm_in_chime_category_selectable():
    """cm_in が ChimeCategory の選択肢にある (無いと studio で音源割付不可=恒久無音)。"""
    from core.models import ChimeCategory

    assert "cm_in" in ChimeCategory.values


def test_fire_cm_bundle_fires_cm_in_chime_when_configured(channel):
    """cm_in チャイムを設定済みなら fire_cm_bundle が layer41 チャイム overlay を併発する。
    未設定なら no-op (無音=任意) — 既存の他テストが chime 無しで通ることが担保。"""
    from core.models import ChannelChime, ChimeCategory, ChimeSound

    snd = ChimeSound.objects.create(name="CM入りSE", r2_key="chime/lib/cmin.mp3")
    ChannelChime.objects.create(channel=channel, category=ChimeCategory.CM_IN, sound=snd)
    fire_cm_bundle(channel, _cm_bundle(), discriminator="live_cue:1")
    chime = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_layer="41",
        params__overlay_op="show",
    )
    assert chime.exists(), "cm_in 設定済みなのに CM 入りチャイム(layer41)が出ていない"
    assert chime.first().params.get("overlay_clip") == snd.clip
