# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""タイムキーパー (docs/timekeeper-live.md) Phase 0: 読み取り専用の集約 ctx。新規モデル無し。

now/next の on-air 解決規則は core.now_playing.resolve_on_air_event と同一 (overlay/transition/
clear_slate 除外) を再利用する。health は api.routers.admin_ops 側で既存 core.ops_views._health_ctx
と合成する (ops_status と同じ配線)。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from core.now_playing import _NON_CONTENT_ACTIONS, resolve_on_air_event
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import LiveCue, LiveCueKind, LiveCueState, Program, ProgramType

_KIND_BY_ACTION = {
    PlayoutAction.CUT_LIVE: "line",
    PlayoutAction.PLAY_ASSET: "line",
    PlayoutAction.PLAY_CM: "cm",
    PlayoutAction.PLAY_CM_BUNDLE: "cm",
    PlayoutAction.PLAY_FILLER: "filler",
    PlayoutAction.PLAY_SLATE: "slate",
    PlayoutAction.PLAY_VT: "vt",
}


def _kind(event) -> str:
    return _KIND_BY_ACTION.get(event.action, "none") if event else "none"


def _label(event) -> str:
    """CM系のaction判定を program_id チェックより先に行う。resolver が生成する PLAY_CM/
    PLAY_CM_BUNDLE イベントも program_id が張られる (放確の逆引き用) ため、program_id を
    先に見ると常に番組タイトルが勝ってしまい CM 広告主名に到達できない。"""
    if event is None:
        return ""
    if event.action == PlayoutAction.PLAY_CM_BUNDLE:
        return event.cm_bundle.name if event.cm_bundle_id and event.cm_bundle else "CM"
    if event.action == PlayoutAction.PLAY_CM:
        p = event.params or {}
        return p.get("advertiser") or (
            event.asset.title if event.asset_id and event.asset else "CM"
        )
    if event.action == PlayoutAction.PLAY_VT:
        return event.asset.title if event.asset_id and event.asset else "VT"
    if event.program_id and event.program:
        return event.program.title
    if event.action == PlayoutAction.PLAY_ASSET and event.asset_id and event.asset:
        return event.asset.title
    if event.action == PlayoutAction.PLAY_FILLER:
        return event.asset.title if event.asset_id and event.asset else "フィラー"
    if event.action == PlayoutAction.PLAY_SLATE:
        return "SLATE"
    return ""


def _bundle_total_ms(event) -> int | None:
    """op_cm_in が積む "cm/<id>:<dur_ms>,..." 形式の clips param から合計尺を復元。"""
    total, found = 0, False
    for part in ((event.params or {}).get("clips") or "").split(","):
        if ":" not in part:
            continue
        try:
            total += int(part.rsplit(":", 1)[1])
            found = True
        except ValueError:
            continue
    return total if found else None


def _segment_end_at(event, next_event) -> int | None:
    if event is None:
        return None
    if event.action == PlayoutAction.PLAY_CM:
        dur = (event.params or {}).get("duration_ms")
        if dur is not None:
            return int((event.scheduled_at + timedelta(milliseconds=int(dur))).timestamp())
    if event.action == PlayoutAction.PLAY_CM_BUNDLE:
        total = _bundle_total_ms(event)
        if total is not None:
            return int((event.scheduled_at + timedelta(milliseconds=total)).timestamp())
    if event.action == PlayoutAction.PLAY_VT:
        p = event.params or {}
        try:
            dur = int(p.get("out_ms", 0)) - int(p.get("in_ms", 0))
        except (TypeError, ValueError):
            dur = None
        if dur is not None:
            return int((event.scheduled_at + timedelta(milliseconds=dur)).timestamp())
    if next_event is not None:
        return int(next_event.scheduled_at.timestamp())
    return None


def _current_program(channel, now) -> Program | None:
    """今この瞬間を覆う編成 Program (live/recorded 問わず)。ops_views._now_playing_ctx.live_program
    は type=LIVE 限定のため録画オンエア中は None になり流用できない — ここで別途解決する。"""
    return (
        Program.objects.filter(channel=channel, start_at__lte=now, end_at__gt=now)
        .select_related("live_rundown")
        .order_by("-start_at")
        .first()
    )


def _cm_remaining(program, cues: list[LiveCue] | None = None) -> dict:
    """録画番組は ad_break→PLAY_CM の事前スケジュール済イベントから集計。生番組は LiveRundown が
    あれば cues (pending な kind=cm) から集計 (Phase 1)。rundown が無い生番組は tracked=False
    (「0本」ではなく「そもそも義務台帳が無い」ことを明示)。"""
    if program is None:
        return {"count": 0, "seconds": 0, "tracked": False}
    if program.type == ProgramType.RECORDED:
        count = seconds = 0
        for ev in PlayoutEvent.objects.filter(
            program=program, action=PlayoutAction.PLAY_CM, status=PlayoutStatus.SCHEDULED
        ):
            count += 1
            seconds += int((ev.params or {}).get("duration_ms") or 0) // 1000
        return {"count": count, "seconds": seconds, "tracked": True}
    if cues is not None:
        return _cue_counted(cues, LiveCueKind.CM)
    return {"count": 0, "seconds": 0, "tracked": False}


def _cue_counted(cues: list[LiveCue], kind: str) -> dict:
    """LiveCue リストから pending な kind の本数/秒を集計 (メモリ内 — 番組あたり高々数十行)。"""
    pending = [c for c in cues if c.kind == kind and c.state == LiveCueState.PENDING]
    return {
        "count": len(pending),
        "seconds": sum(c.planned_duration_ms for c in pending) // 1000,
        "tracked": True,
    }


def _vt_remaining(program, cues: list[LiveCue] | None) -> dict:
    """生番組の VT(録画セグメント)残数/秒 (義務台帳の一種)。生番組かつ rundown ありのみ集計可能。"""
    if program is None or program.type != ProgramType.LIVE or cues is None:
        return {"count": 0, "seconds": 0, "tracked": False}
    return _cue_counted(cues, LiveCueKind.VT)


def _next_cue_at(cues: list[LiveCue] | None, program_start, kind: str) -> int | None:
    """program.start_at + それ以前の cue の planned_duration_ms 累積 (助言・実ドリフト無視)。"""
    if not cues:
        return None
    offset_ms = 0
    for c in cues:
        if c.state == LiveCueState.PENDING and c.kind == kind:
            return int((program_start + timedelta(milliseconds=offset_ms)).timestamp())
        offset_ms += c.planned_duration_ms
    return None


def _rundown_payload(rundown, cues: list[LiveCue] | None, program) -> dict | None:
    """進行表(cues)の参照タイムライン投影。id を含む (フロントの per-cue アクションボタン用)。"""
    if rundown is None:
        return None
    cues = cues or []
    rows = []
    offset_ms = 0
    for c in cues:
        rows.append(
            {
                "id": c.id,
                "seq": c.seq,
                "kind": c.kind,
                "label": c.label or "",
                "planned_at": int(program.start_at.timestamp()) + offset_ms // 1000,
                "planned_dur": c.planned_duration_ms,
                "state": c.state,
                "auto_fire": c.auto_fire,
            }
        )
        offset_ms += c.planned_duration_ms
    planned_total_ms = sum(c.planned_duration_ms for c in cues)
    slot_ms = int((program.end_at - program.start_at).total_seconds() * 1000)
    return {
        "program_id": program.id,
        "cues": rows,
        "planned_total_ms": planned_total_ms,
        "over_under_ms": planned_total_ms - slot_ms,
    }


def timekeeper_ctx(channel, now=None) -> dict:
    now = now or timezone.now()
    event = resolve_on_air_event(channel, now)
    next_event = (
        PlayoutEvent.objects.filter(
            channel=channel, scheduled_at__gt=now, status=PlayoutStatus.SCHEDULED
        )
        .exclude(action__in=_NON_CONTENT_ACTIONS)
        .select_related("program", "asset", "cm_bundle")
        .order_by("scheduled_at")
        .first()
    )
    program = _current_program(channel, now)
    next_program = (
        Program.objects.filter(channel=channel, start_at__gt=now).order_by("start_at").first()
        if program is None
        else None
    )
    # Channel.next_on_air は別機能 (放送休止・#22) 側の追加予定 API。未導入の環境でも壊れないよう
    # 存在チェックする (無ければ next_program_at のみで代替)。
    _next_on_air_fn = getattr(channel, "next_on_air", None)
    next_on_air = _next_on_air_fn(now) if program is None and callable(_next_on_air_fn) else None

    # 生番組の LiveRundown/LiveCue (Phase 1)。OneToOne 逆参照の RelatedObjectDoesNotExist は
    # AttributeError を継承するため getattr(..., None) で安全に無しを表現できる (next_on_air と同じ idiom)。
    rundown = None
    cues = None
    if program is not None and program.type == ProgramType.LIVE:
        rundown = getattr(program, "live_rundown", None)
        if rundown is not None:
            cues = list(rundown.cues.order_by("seq"))

    if program is not None:
        state = "onair"
    elif next_program is not None or next_on_air is not None:
        state = "pre"
    else:
        state = "post"

    return {
        "server_now": int(now.timestamp()),
        "broadcast": {
            "state": state,
            "program_id": program.id if program else None,
            "program_type": program.type if program else "",
            "title": program.title if program else "",
            "start_at": int(program.start_at.timestamp()) if program else None,
            "end_at": int(program.end_at.timestamp()) if program else None,
            "next_program_at": int(next_program.start_at.timestamp()) if next_program else None,
            "next_on_air": int(next_on_air.timestamp()) if next_on_air else None,
        },
        "now": {
            "kind": _kind(event),
            "label": _label(event),
            "segment_end_at": _segment_end_at(event, next_event),
        },
        "next": {
            "kind": _kind(next_event),
            "label": _label(next_event),
            "at": int(next_event.scheduled_at.timestamp()) if next_event else None,
        },
        "cm_remaining": _cm_remaining(program, cues),
        "vt_remaining": _vt_remaining(program, cues),
        "next_cm_at": _next_cue_at(cues, program.start_at, LiveCueKind.CM) if program else None,
        "next_section_at": (
            _next_cue_at(cues, program.start_at, LiveCueKind.SECTION) if program else None
        ),
        "rundown": _rundown_payload(rundown, cues, program) if program else None,
    }
