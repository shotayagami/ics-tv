# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-8): 運用 ops ダッシュボードの realtime status admin API。

staff_auth ゲート + 4 パネル集約 (on-air/health/as-run/notifications) の JSON 化。送出操作自体は
既存 ops エンドポイント (tests/api/test_ops*) の責務。ここは status 集約の構造を検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from core.models import LiveSource, Notification, NotificationSeverity
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import Program, ProgramType

_URL = "/api/v1/admin/ops/{slug}/status"
_TK_URL = "/api/v1/admin/ops/{slug}/timekeeper"


def test_ops_requires_auth(http_client, channel, db):
    assert http_client.get(_URL.format(slug=channel.slug)).status_code == 401


def test_ops_unknown_channel_404(staff_client, db):
    assert staff_client.get(_URL.format(slug="nope")).status_code == 404


def test_ops_status_empty(staff_client, channel, db):
    d = staff_client.get(_URL.format(slug=channel.slug)).json()
    assert d["channel"]["slug"] == channel.slug
    assert any(c["slug"] == channel.slug for c in d["channels"])
    assert d["health"]["online"] is False  # agent 無し
    assert d["on_air"] is None and d["asrun"] == []
    assert d["notifications_count"] == 0 and d["notifications"] == []
    assert d["analytics_url"] == f"/ops/ch/{channel.slug}/analytics/"  # 視聴計測 (旧画面) へ到達可


def test_ops_notifications(staff_client, channel, db):
    Notification.objects.create(
        channel=channel,
        severity=NotificationSeverity.WARN,
        kind="cm_stock_low",
        message="在庫不足テスト",
    )
    Notification.objects.create(
        channel=None, severity=NotificationSeverity.INFO, kind="info", message="全体通知"
    )  # channel=NULL は全 ch 宛
    d = staff_client.get(_URL.format(slug=channel.slug)).json()
    msgs = {n["message"] for n in d["notifications"]}
    assert "在庫不足テスト" in msgs and "全体通知" in msgs
    assert d["notifications_count"] == 2
    row = next(n for n in d["notifications"] if n["message"] == "在庫不足テスト")
    assert row["severity"] == "warn" and row["kind"] == "cm_stock_low"


# ---- タイムキーパー (docs/timekeeper-live.md Phase 0) ----


def _rec_program(channel, asset, start, end, title="番組"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=end,
        asset=asset,
    )


def _live_program(channel, live_source, start, end, title="生"):
    return Program.objects.create(
        channel=channel,
        type=ProgramType.LIVE,
        title=title,
        start_at=start,
        end_at=end,
        live_source=live_source,
    )


def _live_source():
    return LiveSource.objects.create(name="src", rtmp_app="live", rtmp_key="ch1")


def test_timekeeper_requires_auth(http_client, channel, db):
    assert http_client.get(_TK_URL.format(slug=channel.slug)).status_code == 401


def test_timekeeper_unknown_channel_404(staff_client, db):
    assert staff_client.get(_TK_URL.format(slug="nope")).status_code == 404


def test_timekeeper_empty_channel_defaults(staff_client, channel, db):
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["broadcast"]["state"] == "post"
    assert d["broadcast"]["program_id"] is None
    assert d["now"]["kind"] == "none"
    assert d["next"]["kind"] == "none"
    assert d["cm_remaining"] == {"count": 0, "seconds": 0, "tracked": False}
    assert d["health"]["online"] is False


def test_timekeeper_recorded_on_air_cm_remaining(staff_client, channel, asset_ready, db):
    now = timezone.now()
    prog = _rec_program(
        channel, asset_ready, now - timedelta(minutes=10), now + timedelta(minutes=50)
    )
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now + timedelta(minutes=5),
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.SCHEDULED,
        program=prog,
        params={"duration_ms": 15000},
    )
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now + timedelta(minutes=15),
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.SCHEDULED,
        program=prog,
        params={"duration_ms": 30000},
    )
    PlayoutEvent.objects.create(  # 発火済み (DONE) はカウント対象外
        channel=channel,
        scheduled_at=now - timedelta(minutes=5),
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.DONE,
        program=prog,
        params={"duration_ms": 20000},
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["broadcast"]["state"] == "onair"
    assert d["broadcast"]["program_type"] == "recorded"
    assert d["cm_remaining"]["count"] == 2
    assert d["cm_remaining"]["tracked"] is True
    assert d["cm_remaining"]["seconds"] == 45  # 15+30秒。DONE 分の20秒は含まない


def test_timekeeper_now_label_shows_advertiser_not_program_title_during_cm(
    staff_client, channel, asset_ready, db
):
    """resolver が生成する PLAY_CM は program_id が張られる (放確の逆引き用) — _label() が
    program_id を CM 判定より先に見ると常に番組タイトルが勝ってしまう回帰を防ぐ。"""
    now = timezone.now()
    prog = _rec_program(
        channel,
        asset_ready,
        now - timedelta(minutes=10),
        now + timedelta(minutes=50),
        title="夕方の番組",
    )
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=now - timedelta(seconds=5),
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.EXECUTING,
        program=prog,  # resolver.emit_recorded は PLAY_CM にも program_id をセットする
        params={"duration_ms": 15000, "advertiser": "サンプル広告主"},
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["now"]["kind"] == "cm"
    assert d["now"]["label"] == "サンプル広告主"


def test_timekeeper_zero_duration_cm_segment_end_not_dropped(
    staff_client, channel, asset_ready, db
):
    """duration_ms=0 (未probeのCM素材等) を「無し」と誤判定して次イベント時刻にフォールバック
    しないことの回帰テスト (falsy-zero バグ)。"""
    now = timezone.now()
    prog = _rec_program(
        channel, asset_ready, now - timedelta(minutes=10), now + timedelta(minutes=50)
    )
    scheduled_at = now - timedelta(seconds=5)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=scheduled_at,
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.EXECUTING,
        program=prog,
        params={"duration_ms": 0, "advertiser": "尺不明"},
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["now"]["kind"] == "cm"
    assert d["now"]["segment_end_at"] == int(scheduled_at.timestamp())


def test_timekeeper_health_layers_and_yt_slot_status_populated(staff_client, channel, db):
    """ops_status と同じ配線 (_health_ctx) から layers/yt_slot_status を導出すること
    (ハードコードされた空値に戻す回帰を防ぐ)。"""
    from playout.models import AgentStatus
    from youtube.models import YoutubeSlot, YtSlotStatus

    now = timezone.now()
    AgentStatus.objects.create(
        channel=channel,
        last_heartbeat_at=now,
        layers=[{"layer": 40, "role": "telop", "content": "速報テスト"}],
    )
    YoutubeSlot.objects.create(
        channel=channel,
        window_start=now - timedelta(minutes=30),
        window_end=now + timedelta(minutes=90),
        status=YtSlotStatus.LIVE,
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["health"]["online"] is True
    assert d["health"]["layers"] == [{"layer": 40, "role": "telop", "content": "速報テスト"}]
    assert d["health"]["yt_slot_status"] == "live"


def test_timekeeper_live_on_air_cm_not_tracked(staff_client, channel, db):
    """LiveRundown が無い生番組は tracked=False (rundown 未作成時のフォールバック。#25 Phase 1)。

    rundown ありの場合は test_timekeeper_live_with_rundown_cm_vt_tracked が別途 tracked=True を検証する。
    """
    now = timezone.now()
    ls = _live_source()
    prog = _live_program(channel, ls, now - timedelta(minutes=5), now + timedelta(minutes=55))
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=prog.start_at,
        action=PlayoutAction.CUT_LIVE,
        status=PlayoutStatus.DONE,
        program=prog,
        live_source=ls,
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["broadcast"]["program_type"] == "live"
    assert d["now"]["kind"] == "line"
    assert d["cm_remaining"] == {"count": 0, "seconds": 0, "tracked": False}
    assert d["vt_remaining"] == {"count": 0, "seconds": 0, "tracked": False}
    assert d["next_cm_at"] is None
    assert d["next_section_at"] is None
    assert d["rundown"] is None


def test_timekeeper_live_with_rundown_cm_vt_tracked(staff_client, channel, db):
    """LiveRundown ありの生番組: cm_remaining/vt_remaining は pending 行のみを集計し、
    next_cm_at/next_section_at は最初の pending cue の予定位置を指し、rundown payload の
    id/尺合計/枠との過不足が期待通りであること (#25 Phase 1 義務台帳)。"""
    from scheduling.models import LiveCue, LiveCueKind, LiveCueState, LiveRundown

    now = timezone.now()
    ls = _live_source()
    start = now - timedelta(minutes=10)
    end = now + timedelta(minutes=50)  # 60分枠
    prog = _live_program(channel, ls, start, end)
    rundown = LiveRundown.objects.create(program=prog)

    c1 = LiveCue.objects.create(
        rundown=rundown,
        seq=1,
        kind=LiveCueKind.SECTION,
        label="オープニング",
        planned_duration_ms=300_000,  # 5分・aired
        state=LiveCueState.AIRED,
    )
    c2 = LiveCue.objects.create(
        rundown=rundown,
        seq=2,
        kind=LiveCueKind.CM,
        planned_duration_ms=30_000,
        state=LiveCueState.PENDING,
    )  # 最初の pending CM
    c3 = LiveCue.objects.create(
        rundown=rundown,
        seq=3,
        kind=LiveCueKind.VT,
        label="VTR1",
        planned_duration_ms=180_000,  # 3分・aired (pending VT のカウント対象外)
        state=LiveCueState.AIRED,
    )
    c4 = LiveCue.objects.create(
        rundown=rundown,
        seq=4,
        kind=LiveCueKind.SECTION,
        label="本編2",
        planned_duration_ms=600_000,  # 10分・最初の pending 本編
        state=LiveCueState.PENDING,
    )
    c5 = LiveCue.objects.create(
        rundown=rundown,
        seq=5,
        kind=LiveCueKind.CM,
        planned_duration_ms=15_000,
        state=LiveCueState.PENDING,
    )
    c6 = LiveCue.objects.create(
        rundown=rundown,
        seq=6,
        kind=LiveCueKind.VT,
        label="VTR2",
        planned_duration_ms=120_000,  # 2分・唯一の pending VT
        state=LiveCueState.PENDING,
    )
    c7 = LiveCue.objects.create(
        rundown=rundown,
        seq=7,
        kind=LiveCueKind.CM,
        planned_duration_ms=20_000,
        state=LiveCueState.SKIPPED,
    )  # skipped はどちらの集計にも含まれない

    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["broadcast"]["program_type"] == "live"

    # pending CM = c2(30s) + c5(15s)。skipped の c7(20s) は含まない
    assert d["cm_remaining"] == {"count": 2, "seconds": 45, "tracked": True}
    # pending VT = c6(120s) のみ (c3 は aired)
    assert d["vt_remaining"] == {"count": 1, "seconds": 120, "tracked": True}

    # next_cm_at: 最初の pending cm=c2 の予定開始 = start + c1(5分)
    assert d["next_cm_at"] == int((start + timedelta(milliseconds=300_000)).timestamp())
    # next_section_at: 最初の pending section=c4 の予定開始 = start + c1+c2+c3 (5分+30秒+3分)
    assert d["next_section_at"] == int(
        (start + timedelta(milliseconds=300_000 + 30_000 + 180_000)).timestamp()
    )

    rd = d["rundown"]
    assert rd["program_id"] == prog.id
    assert [row["id"] for row in rd["cues"]] == [c1.id, c2.id, c3.id, c4.id, c5.id, c6.id, c7.id]
    assert [row["state"] for row in rd["cues"]] == [
        "aired",
        "pending",
        "aired",
        "pending",
        "pending",
        "pending",
        "skipped",
    ]
    planned_total = 300_000 + 30_000 + 180_000 + 600_000 + 15_000 + 120_000 + 20_000
    assert rd["planned_total_ms"] == planned_total
    slot_ms = int((end - start).total_seconds() * 1000)
    assert rd["over_under_ms"] == planned_total - slot_ms


def test_timekeeper_now_label_shows_vt_asset_title_not_program_title(
    staff_client, channel, asset_ready, db
):
    """PLAY_VT イベントも program_id が張られうる (生番組内ロール) — _label() が program_id を
    VT 判定より先に見ると常に番組タイトルが勝ってしまう回帰を防ぐ (#25 Batch C・PLAY_CM と同じ理由)。"""
    ls = LiveSource.objects.create(name="src2", rtmp_app="live", rtmp_key="ch1-2")
    now = timezone.now()
    prog = _live_program(
        channel, ls, now - timedelta(minutes=10), now + timedelta(minutes=50), title="生番組本体"
    )
    scheduled_at = now - timedelta(seconds=5)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=scheduled_at,
        action=PlayoutAction.PLAY_VT,
        status=PlayoutStatus.EXECUTING,
        program=prog,
        asset=asset_ready,
        params={"clip": f"asset/{asset_ready.id}", "in_ms": 0, "out_ms": 90_000},
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["now"]["kind"] == "vt"
    assert d["now"]["label"] == asset_ready.title
    assert d["now"]["segment_end_at"] == int(
        (scheduled_at + timedelta(milliseconds=90_000)).timestamp()
    )


def test_timekeeper_pre_broadcast_standby(staff_client, channel, asset_ready, db):
    now = timezone.now()
    start = now + timedelta(minutes=10)
    _rec_program(channel, asset_ready, start, start + timedelta(hours=1))
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["broadcast"]["state"] == "pre"
    assert d["broadcast"]["program_id"] is None
    assert d["broadcast"]["next_program_at"] == int(start.timestamp())


def test_timekeeper_cm_bundle_segment_end(staff_client, channel, db):
    now = timezone.now()
    scheduled_at = now - timedelta(seconds=5)
    PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=scheduled_at,
        action=PlayoutAction.PLAY_CM_BUNDLE,
        status=PlayoutStatus.EXECUTING,
        params={"clips": "cm/1:20000,cm/2:10000"},
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["now"]["kind"] == "cm"
    assert d["now"]["segment_end_at"] == int((scheduled_at + timedelta(seconds=30)).timestamp())


def test_timekeeper_next_event_populated(staff_client, channel, db):
    now = timezone.now()
    scheduled_at = now + timedelta(minutes=5)
    ev = PlayoutEvent.objects.create(
        channel=channel,
        scheduled_at=scheduled_at,
        action=PlayoutAction.PLAY_CM,
        status=PlayoutStatus.SCHEDULED,
        params={"duration_ms": 15000},
    )
    d = staff_client.get(_TK_URL.format(slug=channel.slug)).json()
    assert d["next"]["kind"] == "cm"
    assert d["next"]["at"] == int(ev.scheduled_at.timestamp())
