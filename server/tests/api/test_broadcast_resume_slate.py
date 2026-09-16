# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""放送休止明けのスレート解除 (CLEAR_SLATE) 回帰防止。

layer 90 (スレート) は layer 10 (本線) と独立レイヤなので、休止明けに本線側 (emit_filler) が
再開しても CLEAR_SLATE を明示発行しない限りスレートが本線の上に残り続ける (本線は復帰している
のに画面がスレート固着のまま=実運用で発覚した重大バグ)。resolve() が休止明けに CLEAR_SLATE を
発行すること、かつ resolve 再実行や運用者の手動/緊急スレートを奪い返さないことを確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus

pytestmark = pytest.mark.django_db(transaction=True)


def _set_broadcast_windows(channel):
    # 16:00-24:00 JST 運用 (07:00-15:00 UTC)。
    channel.broadcast_windows = [{"start": "16:00", "end": "24:00"}]
    channel.save(update_fields=["broadcast_windows"])


def test_resolve_emits_clear_slate_after_off_air_slate(channel):
    """休止明け (on-air 区間に突入済) の resolve は CLEAR_SLATE を発行する (本不具合の根治)。"""
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    # 15:50 JST (休止中) に resolver 管轄の off_air スレートが出ていた想定。
    off_air_at = datetime(2026, 7, 3, 6, 50, tzinfo=UTC)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=off_air_at,
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "until": "2026-07-03T07:00:00+00:00", "loop": True},
    )
    # 16:10 JST = 放送再開から 10 分後 (今朝の事故と同型: resolve が catch-up で気づく)。
    now = datetime(2026, 7, 3, 7, 10, tzinfo=UTC)

    resolve(channel, now, now + timedelta(hours=1))

    clears = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE)
    assert clears.count() == 1
    ev = clears.first()
    assert ev.status == PlayoutStatus.SCHEDULED
    assert ev.scheduled_at <= now


def test_resolve_does_not_reemit_clear_slate_on_next_cycle(channel):
    """一度解除したら次の resolve 周期で再発行しない (毎周期の re-issue はスレート層こそ無害だが
    運用者の手動スレートを奪い返す事故につながるため、明示的に止めている)。"""
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    off_air_at = datetime(2026, 7, 3, 6, 50, tzinfo=UTC)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=off_air_at,
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "loop": True},
    )
    now = datetime(2026, 7, 3, 7, 10, tzinfo=UTC)
    resolve(channel, now, now + timedelta(hours=1))
    assert (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE).count() == 1
    )

    # 次の beat 周期 (5分後) に再解決しても CLEAR_SLATE は増えない。
    later = now + timedelta(minutes=5)
    resolve(channel, later, later + timedelta(hours=1))
    assert (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE).count() == 1
    )


def test_resolve_does_not_clobber_manual_slate_during_on_air(channel):
    """休止明け解除が済んだ後に運用者が緊急スレートを出しても、次の resolve が消してしまわない。"""
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    off_air_at = datetime(2026, 7, 3, 6, 50, tzinfo=UTC)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=off_air_at,
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "loop": True},
    )
    now = datetime(2026, 7, 3, 7, 10, tzinfo=UTC)
    resolve(channel, now, now + timedelta(hours=1))  # 休止明け解除 (CLEAR_SLATE 1件)

    # 17:00 JST、運用者が緊急スレートを手動投入 (off_air param 無し = 手動系統)。
    manual_at = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=manual_at,
        action=PlayoutAction.PLAY_SLATE,
        params={"reason": "manual_emergency"},
    )

    # 次の beat 周期でまだ同じ on-air 窓内 (24:00 まで) を再解決。
    later = manual_at + timedelta(minutes=5)
    resolve(channel, later, later + timedelta(hours=1))

    # 手動スレートより後の CLEAR_SLATE が新たに出ていないこと (奪い返し無し)。
    assert not PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.CLEAR_SLATE,
        scheduled_at__gt=manual_at,
    ).exists()
    # 手動スレート自体も CANCELLED にされていないこと。
    manual_ev = PlayoutEvent.objects.get(scheduled_at=manual_at, action=PlayoutAction.PLAY_SLATE)
    assert manual_ev.status != PlayoutStatus.CANCELLED


def _recorded(channel, asset_ready, start, end):
    from scheduling.models import Program, ProgramType

    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title="朝の回",
        start_at=start,
        end_at=end,
        asset=asset_ready,
    )


def test_clear_slate_when_program_starts_at_window_open(channel, asset_ready):
    """窓オープンちょうど (休止明け=番組開始) の番組でも CLEAR_SLATE が番組開始に出る。

    直前ギャップは全区間 off-air で on-air フィラー区間が存在しないため、ギャップ側の解除
    経路が働かず番組終了までスレートが被りっぱなしになる回帰 (2026-07-06 6時スタート回の事故)。
    """
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    # 15:00 JST (休止中) に off_air スレート発行済。
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC),
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "loop": True},
    )
    # 番組は 16:00 JST (窓オープン) ちょうどに開始。
    prog_start = datetime(2026, 7, 3, 7, 0, tzinfo=UTC)
    _recorded(channel, asset_ready, prog_start, prog_start + timedelta(hours=1))
    # 15:50 JST (休止中・番組開始前) に resolve。
    now = datetime(2026, 7, 3, 6, 50, tzinfo=UTC)

    resolve(channel, now, now + timedelta(hours=3))

    clears = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE).exclude(
        status=PlayoutStatus.CANCELLED
    )
    assert clears.count() == 1
    assert clears.first().scheduled_at == prog_start


def test_clear_slate_catchup_when_program_already_started(channel, asset_ready):
    """窓オープン=番組開始の編成で、番組開始後に初めて resolve が走っても即時解除する。"""
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC),
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "loop": True},
    )
    prog_start = datetime(2026, 7, 3, 7, 0, tzinfo=UTC)
    _recorded(channel, asset_ready, prog_start, prog_start + timedelta(hours=1))
    # 番組開始 10 分後に catch-up。
    now = datetime(2026, 7, 3, 7, 10, tzinfo=UTC)

    resolve(channel, now, now + timedelta(hours=3))

    clears = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE).exclude(
        status=PlayoutStatus.CANCELLED
    )
    assert clears.count() == 1
    assert clears.first().scheduled_at <= now


def test_future_clear_slate_survives_next_resolve_cycle(channel):
    """事前発行した未来の CLEAR_SLATE が次の resolve 周期で CANCELLED にされない (フラップ根治)。

    旧実装は「SCHEDULED の未来 CLEAR がある→解除済み」と再発行をやめ、_commit が tombstone 化
    →次周期で復活…をビートごとに繰り返し、休止明け時刻にちょうど CANCELLED 側だと agent が
    実行せずスレートが残った (最大 1 ビート＝5 分)。
    """
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC),
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "loop": True},
    )
    # 窓オープン (16:00 JST = 07:00 UTC) 前に 2 周期 resolve。
    for minute in (50, 55):
        now = datetime(2026, 7, 3, 6, minute, tzinfo=UTC)
        resolve(channel, now, now + timedelta(hours=3))

    clears = PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CLEAR_SLATE)
    assert clears.count() == 1
    ev = clears.first()
    assert ev.status == PlayoutStatus.SCHEDULED
    assert ev.scheduled_at == datetime(2026, 7, 3, 7, 0, tzinfo=UTC)


def test_airing_program_not_replaced_by_filler_on_recycle(channel, asset_ready):
    """放送中に開始済みの番組が resolve 再実行で除外されない (フィラー被せ発行の回帰防止)。

    番組スキップ判定を t0 クリップ済みの on-air 区間で行うと、窓中に正常開始した番組も
    start_at < now になった瞬間に編成から消え、残り尺がギャップ扱い→フィラーが本編を奪う。
    """
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    # 17:00-18:00 JST (窓中) の番組。放送 30 分経過時点で resolve。
    prog_start = datetime(2026, 7, 3, 8, 0, tzinfo=UTC)
    prog_end = datetime(2026, 7, 3, 9, 0, tzinfo=UTC)
    _recorded(channel, asset_ready, prog_start, prog_end)
    now = datetime(2026, 7, 3, 8, 30, tzinfo=UTC)

    resolve(channel, now, now + timedelta(hours=3))

    fillers_during_program = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.PLAY_FILLER,
        scheduled_at__gte=now,
        scheduled_at__lt=prog_end,
    ).exclude(status=PlayoutStatus.CANCELLED)
    assert not fillers_during_program.exists()


def test_resolve_no_clear_slate_when_broadcast_windows_unset(channel, asset_ready):
    """broadcast_windows 未設定 (24h 運用) では従来通り CLEAR_SLATE を発行しない (回帰なし)。"""
    from scheduling.resolver import resolve

    now = datetime(2026, 7, 3, 7, 10, tzinfo=UTC)
    resolve(channel, now, now + timedelta(hours=1))

    assert not PlayoutEvent.objects.filter(
        channel=channel, action=PlayoutAction.CLEAR_SLATE
    ).exists()


# ---- 休止明け直前のスレート発行ガード (2026-07-21 06:00 の事故) ----


def _off_air_slates(channel):
    return PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.PLAY_SLATE,
        params__contains={"off_air": True},
    ).exclude(status=PlayoutStatus.CANCELLED)


def test_no_standby_slate_emitted_just_before_window_opens(channel):
    """休止明け直前の周期では off_air スレートを再発行しない (競合の発生源を断つ)。

    休止中は resolve 周期ごとに「now」でスレートを再発行するが、窓オープン直前の 1 発は
    agent への到達 + ディスパッチ遅延の間に休止明けの CLEAR_SLATE に追い越され、復帰済みの
    本線の上へ再点灯して固着する (2026-07-21: 05:59:59 発行 → 06:00:01 実行 → 2h21m 固着)。
    """
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    # 15:59:59 JST = 窓オープン (16:00 JST) の 1 秒前。
    now = datetime(2026, 7, 3, 6, 59, 59, tzinfo=UTC)

    resolve(channel, now, now + timedelta(hours=3))

    assert not _off_air_slates(channel).filter(scheduled_at=now).exists()


def test_standby_slate_still_emitted_with_enough_margin(channel):
    """休止明けまで余裕がある周期では従来どおり off_air スレートを発行する (回帰なし)。"""
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    # 15:55 JST = 窓オープンまで 5 分。
    now = datetime(2026, 7, 3, 6, 55, tzinfo=UTC)

    resolve(channel, now, now + timedelta(hours=3))

    slate = _off_air_slates(channel).get(scheduled_at=now)
    # until は窓オープン時刻 (JST 表記だが指す瞬間は同一)。
    assert datetime.fromisoformat(slate.params["until"]) == datetime(2026, 7, 3, 7, 0, tzinfo=UTC)


def test_resolve_reissues_clear_when_slate_executed_after_clear(channel):
    """CLEAR より後に実行された休止スレートを検出し、CLEAR_SLATE を再発行する (自己修復)。

    scheduled_at 上は CLEAR(16:00:00) > slate(15:59:59) で「解除済み」に見えるが、実行順は
    逆転しており画面はスレート固着のまま。旧実装は再発行せず、運用者が手動解除するまで
    2h21m 復旧しなかった (2026-07-21)。actual_at で逆転を検出し次周期で自動復旧する。
    """
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    slate_at = datetime(2026, 7, 3, 6, 59, 59, tzinfo=UTC)
    clear_at = datetime(2026, 7, 3, 7, 0, tzinfo=UTC)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=slate_at,
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "until": clear_at.isoformat(), "loop": True},
        status=PlayoutStatus.DONE,
        actual_at=clear_at + timedelta(seconds=1),  # CLEAR の 1 秒後に実行された
    )
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=clear_at,
        action=PlayoutAction.CLEAR_SLATE,
        params={},
        status=PlayoutStatus.DONE,
        actual_at=clear_at,
    )
    # 休止明け 5 分後の次ビート。
    now = clear_at + timedelta(minutes=5)

    resolve(channel, now, now + timedelta(hours=1))

    reissued = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.CLEAR_SLATE,
        scheduled_at__gt=clear_at,
    ).exclude(status=PlayoutStatus.CANCELLED)
    assert reissued.count() == 1
    assert reissued.first().scheduled_at <= now


def test_no_reissue_once_recovery_clear_has_executed(channel):
    """再発行した CLEAR が実行済みなら、以降の周期では再発行しない (無限再発行の防止)。"""
    from scheduling.resolver import resolve

    _set_broadcast_windows(channel)
    clear_at = datetime(2026, 7, 3, 7, 0, tzinfo=UTC)
    slate_exec_at = clear_at + timedelta(seconds=1)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=datetime(2026, 7, 3, 6, 59, 59, tzinfo=UTC),
        action=PlayoutAction.PLAY_SLATE,
        params={"off_air": True, "until": clear_at.isoformat(), "loop": True},
        status=PlayoutStatus.DONE,
        actual_at=slate_exec_at,
    )
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=clear_at,
        action=PlayoutAction.CLEAR_SLATE,
        params={},
        status=PlayoutStatus.DONE,
        actual_at=clear_at,
    )
    # 前周期で再発行され、実行も済んだ復旧 CLEAR。
    recovery_at = clear_at + timedelta(minutes=5)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=recovery_at,
        action=PlayoutAction.CLEAR_SLATE,
        params={},
        status=PlayoutStatus.DONE,
        actual_at=recovery_at,
    )
    now = recovery_at + timedelta(minutes=5)

    resolve(channel, now, now + timedelta(hours=1))

    assert not PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.CLEAR_SLATE,
        scheduled_at__gt=recovery_at,
    ).exists()
