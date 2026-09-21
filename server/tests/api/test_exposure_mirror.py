# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""exposure_policy の YTミラー制御イベント (#27、docs/site-only-broadcast.md §4.3)。

emit_exposure_mirror() 単体 + resolve()/_commit() を通した連続区間最適化(フリッカ防止)の
回帰確認が主眼。「同一seg・背中合わせ・同じ必要性」の番組境界では ROUTE(end)/FILLER(start) を
それぞれ1回に間引くこと、exposure_policy 変更時に旧イベントが自動 tombstone 化されることを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.models import LiveSource
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import ExposurePolicy, Program, ProgramType
from scheduling.resolver import (
    YT_MEMBERS_MIRROR_SEG,
    YT_PUBLIC_MIRROR_SEG,
    _exposure_mirror_needs,
    emit_exposure_mirror,
    resolve,
)

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _live_program(
    channel, *, start, end, policy=ExposurePolicy.PUBLIC, title="生番組", fc_required_level=None
):
    ls = LiveSource.objects.create(name=f"OBS-{title}", rtmp_app="live", rtmp_key=title)
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=end,
        live_source=ls,
        exposure_policy=policy,
        fc_required_level=fc_required_level,
    )


# ---- _exposure_mirror_needs ----


def test_needs_public_only_for_non_public_policy():
    assert _exposure_mirror_needs(ExposurePolicy.PUBLIC) == (False, False)
    assert _exposure_mirror_needs(ExposurePolicy.SITE_PUBLIC) == (True, False)
    assert _exposure_mirror_needs(ExposurePolicy.SITE_MEMBERS) == (True, False)
    assert _exposure_mirror_needs(ExposurePolicy.MEMBERS_YT_SITE) == (True, True)


# ---- emit_exposure_mirror: 単体 ----


def test_public_policy_emits_no_mirror_events(channel):
    prog = _live_program(channel, start=BASE, end=BASE + timedelta(hours=1))
    events = emit_exposure_mirror(prog, prev_prog=None, next_prog=None)
    assert events == []


def test_fc_required_level_forces_public_mirror_filler_even_when_policy_is_public(channel):
    """fc_required_level (#27 Phase B) 単独でも公開ミラーはフィラー化する (site-member 軸との

    不整合を防ぐ回帰確認: exposure_policy=public のままファンクラブ ティアだけでゲートした
    番組が、サイトでは隠れるのに公開 YouTube 本線ではそのまま流れる、という穴を塞ぐ)。
    メンバーミラー (yt_members) はファンクラブ ティアと無関係の別軸のため要求しない。
    """
    prog = _live_program(channel, start=BASE, end=BASE + timedelta(hours=1), fc_required_level=0)
    events = emit_exposure_mirror(prog, prev_prog=None, next_prog=None)
    assert len(events) == 2
    by_action = {e.action: e for e in events}
    assert by_action[PlayoutAction.YT_MIRROR_FILLER].scheduled_at == prog.start_at
    assert by_action[PlayoutAction.YT_MIRROR_ROUTE].scheduled_at == prog.end_at
    assert all(e.params["mirror_seg"] == YT_PUBLIC_MIRROR_SEG for e in events)


def test_site_public_isolated_emits_open_and_close(channel):
    prog = _live_program(
        channel, start=BASE, end=BASE + timedelta(hours=1), policy=ExposurePolicy.SITE_PUBLIC
    )
    events = emit_exposure_mirror(prog, prev_prog=None, next_prog=None)
    assert len(events) == 2
    by_action = {e.action: e for e in events}
    assert by_action[PlayoutAction.YT_MIRROR_FILLER].scheduled_at == prog.start_at
    assert by_action[PlayoutAction.YT_MIRROR_ROUTE].scheduled_at == prog.end_at
    assert all(e.params["mirror_seg"] == YT_PUBLIC_MIRROR_SEG for e in events)


def test_members_yt_site_isolated_emits_both_segs(channel):
    prog = _live_program(
        channel, start=BASE, end=BASE + timedelta(hours=1), policy=ExposurePolicy.MEMBERS_YT_SITE
    )
    events = emit_exposure_mirror(prog, prev_prog=None, next_prog=None)
    assert len(events) == 4
    public_events = [e for e in events if e.params["mirror_seg"] == YT_PUBLIC_MIRROR_SEG]
    members_events = [e for e in events if e.params["mirror_seg"] == YT_MEMBERS_MIRROR_SEG]
    assert {e.action for e in public_events} == {
        PlayoutAction.YT_MIRROR_FILLER,
        PlayoutAction.YT_MIRROR_ROUTE,
    }
    # メンバーミラーは公開ミラーと開始/終了アクションが逆(既定 filler→本編開始で route)
    members_by_action = {e.action: e for e in members_events}
    assert members_by_action[PlayoutAction.YT_MIRROR_ROUTE].scheduled_at == prog.start_at
    assert members_by_action[PlayoutAction.YT_MIRROR_FILLER].scheduled_at == prog.end_at


# ---- 連続区間最適化 (フリッカ防止) ----


def test_adjacent_same_policy_suppresses_boundary_duplication(channel):
    """背中合わせ・同じ policy の2番組は境界で1回だけ (open は2本目で省略、close は1本目で省略)。"""
    mid = BASE + timedelta(hours=1)
    prog1 = _live_program(
        channel, start=BASE, end=mid, policy=ExposurePolicy.SITE_MEMBERS, title="p1"
    )
    prog2 = _live_program(
        channel,
        start=mid,
        end=mid + timedelta(hours=1),
        policy=ExposurePolicy.SITE_MEMBERS,
        title="p2",
    )
    ev1 = emit_exposure_mirror(prog1, prev_prog=None, next_prog=prog2)
    ev2 = emit_exposure_mirror(prog2, prev_prog=prog1, next_prog=None)
    # prog1: OPEN のみ (CLOSE は次が継続するため省略)
    assert len(ev1) == 1 and ev1[0].action == PlayoutAction.YT_MIRROR_FILLER
    assert ev1[0].scheduled_at == prog1.start_at
    # prog2: CLOSE のみ (OPEN は前から継続しているため省略)
    assert len(ev2) == 1 and ev2[0].action == PlayoutAction.YT_MIRROR_ROUTE
    assert ev2[0].scheduled_at == prog2.end_at


def test_gap_between_same_policy_programs_emits_full_pairs(channel):
    """隣接していない(ギャップがある)場合は連続区間とみなさず、それぞれ開始/終了を両方出す。"""
    prog1 = _live_program(
        channel,
        start=BASE,
        end=BASE + timedelta(hours=1),
        policy=ExposurePolicy.SITE_MEMBERS,
        title="p1",
    )
    prog2 = _live_program(
        channel,
        start=BASE + timedelta(hours=2),
        end=BASE + timedelta(hours=3),
        policy=ExposurePolicy.SITE_MEMBERS,
        title="p2",
    )
    ev1 = emit_exposure_mirror(prog1, prev_prog=None, next_prog=prog2)
    ev2 = emit_exposure_mirror(prog2, prev_prog=prog1, next_prog=None)
    assert len(ev1) == 2
    assert len(ev2) == 2


def test_adjacent_differing_policy_still_transitions_at_boundary(channel):
    """境界で必要性が変わるなら(members_yt_site → site_public)、変わった軸だけ通常どおり発行する。"""
    mid = BASE + timedelta(hours=1)
    prog1 = _live_program(
        channel, start=BASE, end=mid, policy=ExposurePolicy.MEMBERS_YT_SITE, title="p1"
    )
    prog2 = _live_program(
        channel,
        start=mid,
        end=mid + timedelta(hours=1),
        policy=ExposurePolicy.SITE_PUBLIC,
        title="p2",
    )
    ev1 = emit_exposure_mirror(prog1, prev_prog=None, next_prog=prog2)
    ev2 = emit_exposure_mirror(prog2, prev_prog=prog1, next_prog=None)
    # 公開ミラー(yt_public)は両者とも needs=True で連続 → prog1 は open のみ、prog2 は close のみ
    ev1_public = [e for e in ev1 if e.params["mirror_seg"] == YT_PUBLIC_MIRROR_SEG]
    ev2_public = [e for e in ev2 if e.params["mirror_seg"] == YT_PUBLIC_MIRROR_SEG]
    assert len(ev1_public) == 1 and ev1_public[0].action == PlayoutAction.YT_MIRROR_FILLER
    assert len(ev2_public) == 1 and ev2_public[0].action == PlayoutAction.YT_MIRROR_ROUTE
    # メンバーミラー(yt_members)は prog1=True→prog2=False で不連続 → prog1 が close を出す(open は無し=prev無)
    ev1_members = [e for e in ev1 if e.params["mirror_seg"] == YT_MEMBERS_MIRROR_SEG]
    ev2_members = [e for e in ev2 if e.params["mirror_seg"] == YT_MEMBERS_MIRROR_SEG]
    assert (
        len(ev1_members) == 2
    )  # prev_prog=None なので open も出る、close も (next が必要ないので) 出る
    assert ev2_members == []  # prog2 は members 不要なのでイベント無し


# ---- resolve()/_commit() 統合: DB に反映されるイベント件数の回帰確認 ----


def test_resolve_commits_single_open_close_for_two_adjacent_site_members_programs(channel):
    mid = BASE + timedelta(hours=1)
    _live_program(channel, start=BASE, end=mid, policy=ExposurePolicy.SITE_MEMBERS, title="p1")
    _live_program(
        channel,
        start=mid,
        end=mid + timedelta(hours=1),
        policy=ExposurePolicy.SITE_MEMBERS,
        title="p2",
    )
    resolve(channel, BASE - timedelta(hours=1), BASE + timedelta(hours=3))
    mirror_events = PlayoutEvent.objects.filter(
        channel=channel,
        action__in=[PlayoutAction.YT_MIRROR_FILLER, PlayoutAction.YT_MIRROR_ROUTE],
        status=PlayoutStatus.SCHEDULED,
    ).order_by("scheduled_at")
    assert list(mirror_events.values_list("action", "scheduled_at")) == [
        (PlayoutAction.YT_MIRROR_FILLER, BASE),
        (PlayoutAction.YT_MIRROR_ROUTE, mid + timedelta(hours=1)),
    ]


def test_resolve_tombstones_mirror_events_when_policy_changes_to_public(channel):
    prog = _live_program(
        channel, start=BASE, end=BASE + timedelta(hours=1), policy=ExposurePolicy.SITE_PUBLIC
    )
    resolve(channel, BASE - timedelta(hours=1), BASE + timedelta(hours=2))
    assert PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.YT_MIRROR_FILLER, status=PlayoutStatus.SCHEDULED
    ).exists()

    prog.exposure_policy = ExposurePolicy.PUBLIC
    prog.save(update_fields=["exposure_policy"])
    resolve(channel, BASE - timedelta(hours=1), BASE + timedelta(hours=2))

    assert not PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.YT_MIRROR_FILLER, status=PlayoutStatus.SCHEDULED
    ).exists()
    assert PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.YT_MIRROR_FILLER, status=PlayoutStatus.CANCELLED
    ).exists()
