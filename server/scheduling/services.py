# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""押え (延長) / 巻き (早終い) サービス (docs/operations.md O1・O8)。

運行中の番組の尺を運用画面から変える。押えは後続を 48h 解決窓内で繰り下げ、隙間 (フィラー) で
減衰吸収する。巻きは当該番組の end_at を縮めるだけで後続は触らず、空いた区間は再解決で
フィラーが充填する。いずれも commit 後に当該 ch のみ即時再解決 (resolve_channel_now)。

slug 生成 (slugify_unique) と AudienceForm フィールド / ペイロード検証 (validate_fields,
validate_payload) もここで提供する。
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from scheduling.models import Program, ProgramType
from scheduling.tasks import resolve_channel_now

# --------------------------------------------------------------------------- #
#  Series スラッグ生成                                                         #
# --------------------------------------------------------------------------- #

_RESERVED_SLUGS = {"new", "p", "api", "admin", "static", "media", "favicon.ico"}
_DIGIT_ONLY = re.compile(r"^\d+$")


def slugify_unique(title: str, channel_id: int, exclude_series_id: int | None = None) -> str:
    """title から slug を生成してチャンネル内で一意にして返す。

    - slugify(title) で ASCII 小文字ハイフン化 → 空/数字のみ/予約語なら "program" を既定に使う。
    - チャンネル内衝突時は -2,-3... を付与。
    - exclude_series_id: 自身の更新時など、既存 slug を除外して比較する場合に指定する。
    """
    from scheduling.models import Series

    base = slugify(title) or "program"
    if _DIGIT_ONLY.fullmatch(base) or base in _RESERVED_SLUGS:
        base = "program"
    base = base[:70]  # 連番付与のため 70 文字で切る (max 80)

    candidate = base
    counter = 2
    qs = Series.objects.filter(channel_id=channel_id, slug__gt="")
    if exclude_series_id is not None:
        qs = qs.exclude(pk=exclude_series_id)
    existing = set(qs.values_list("slug", flat=True))
    while candidate in existing:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------- #
#  AudienceForm フィールド / ペイロード検証                                    #
# --------------------------------------------------------------------------- #

_ALLOWED_TYPES = {"text", "textarea", "email", "tel", "select", "date", "checkbox"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TEL_RE = re.compile(r"^[\d\s\-\+\(\)]{7,20}$")


class FieldSchemaError(ValueError):
    """フィールド定義 (fields JSON) が不正。"""


class PayloadValidationError(ValueError):
    """投稿ペイロードがフォーム定義を満たさない。"""


def validate_fields(fields: list[dict]) -> None:
    """AudienceForm.fields の定義リストを検証。FieldSchemaError を raise。"""
    if not isinstance(fields, list):
        raise FieldSchemaError("fields はリストである必要があります")
    keys_seen: set[str] = set()
    for i, f in enumerate(fields):
        label = f"fields[{i}]"
        if not isinstance(f, dict):
            raise FieldSchemaError(f"{label}: dict である必要があります")
        key = f.get("key", "")
        if not key or not isinstance(key, str):
            raise FieldSchemaError(f"{label}: key は必須の文字列です")
        if key in keys_seen:
            raise FieldSchemaError(f"{label}: key '{key}' が重複しています")
        keys_seen.add(key)
        if not f.get("label") or not isinstance(f["label"], str):
            raise FieldSchemaError(f"{label}: label は必須の文字列です")
        ftype = f.get("type", "")
        if ftype not in _ALLOWED_TYPES:
            raise FieldSchemaError(f"{label}: type '{ftype}' は不正です ({_ALLOWED_TYPES})")
        if ftype == "select":
            opts = f.get("options", [])
            if not isinstance(opts, list) or not opts:
                raise FieldSchemaError(f"{label}: select には options(非空リスト)が必要です")


def validate_payload(form, payload: dict) -> None:
    """AudienceForm インスタンスと投稿 payload を照合。PayloadValidationError を raise。

    - required フィールドが未入力または空
    - select フィールドが options 外の値
    - email/tel/date フィールドの書式 (軽い正規表現)
    """
    if not isinstance(payload, dict):
        raise PayloadValidationError("payload は JSON オブジェクトである必要があります")
    for f in form.fields:
        key = f["key"]
        ftype = f.get("type", "text")
        required = bool(f.get("required", False))
        value = payload.get(key, "")
        str_value = str(value).strip() if not isinstance(value, bool) else ""

        if required and ftype != "checkbox" and not str_value:
            raise PayloadValidationError(f"「{f['label']}」は必須です")
        if required and ftype == "checkbox" and not value:
            raise PayloadValidationError(f"「{f['label']}」は必須です")

        if not str_value:
            continue

        if ftype == "select":
            opts = [str(o) for o in f.get("options", [])]
            if str_value not in opts:
                raise PayloadValidationError(f"「{f['label']}」: 選択肢外の値です")
        elif ftype == "email" and not _EMAIL_RE.match(str_value):
            raise PayloadValidationError(
                f"「{f['label']}」: メールアドレスの形式が正しくありません"
            )
        elif ftype == "date" and not _DATE_RE.match(str_value):
            raise PayloadValidationError(
                f"「{f['label']}」: 日付は YYYY-MM-DD 形式で入力してください"
            )
        elif ftype == "tel" and not _TEL_RE.match(str_value):
            raise PayloadValidationError(f"「{f['label']}」: 電話番号の形式が正しくありません")


WINDOW = timedelta(hours=48)  # 解決窓と同じ (シフト対象の上限。O1: 暴走カスケード防止)
GUARD_MS = 60_000  # 終了 60 秒前を切ったら押え不可 (旧イベント発火とのレース回避)


class ExtendRejectedError(Exception):
    """押え (延長) が不可な条件 (終了直前 / 窓内で吸収できない)。"""


class ShortenRejectedError(Exception):
    """巻き (早終い) が不可な条件 (recorded / 開始・現在を下回る)。"""


def _td(ms: int) -> timedelta:
    return timedelta(milliseconds=ms)


def _ms_between(a, b) -> int:
    """a→b のミリ秒 (b>=a 前提。負なら 0)。"""
    return max(0, int((b - a).total_seconds() * 1000))


def _compute_extend_shifts(
    prog: Program, tail: list[Program], delta_ms: int
) -> tuple[list[tuple[Program, int]], int]:
    """後続 (tail, start_at 昇順) の繰り下げ量を算出。各隙間で減衰吸収。

    戻り値: (shifts=[(program, shift_ms)], remaining_ms)。remaining>0 かつ shifts 非空なら吸収不能。
    """
    shifts: list[tuple[Program, int]] = []
    remaining = delta_ms
    cursor = prog.end_at
    for p in tail:
        gap = _ms_between(cursor, p.start_at)  # 直前との隙間 (フィラー)
        remaining = max(0, remaining - gap)
        if remaining == 0:
            break  # この隙間で完全吸収
        shifts.append((p, remaining))
        cursor = p.end_at
    return shifts, remaining


def preview_extend(program_id: int, delta_ms: int) -> dict:
    """押えの影響を読み取り専用で算出 (確認ダイアログ用。DB 変更なし)。

    戻り値: {ok, reason, shifts=[{title, shift_ms}], new_end}。ok=False なら reason に理由。
    """
    prog = Program.objects.get(pk=program_id)
    now = timezone.now()
    if delta_ms <= 0:
        return {"ok": False, "reason": "延長量は正の値である必要があります", "shifts": []}
    if _ms_between(now, prog.end_at) < GUARD_MS:
        return {"ok": False, "reason": "終了直前 (残り 60 秒未満) は延長できません", "shifts": []}
    tail = list(
        Program.objects.filter(
            channel_id=prog.channel_id,
            start_at__gte=prog.end_at,
            start_at__lt=now + WINDOW,
        ).order_by("start_at")
    )
    shifts, remaining = _compute_extend_shifts(prog, tail, delta_ms)
    shift_rows = [{"title": p.title, "shift_ms": s} for p, s in shifts]
    if remaining > 0 and shifts:
        return {
            "ok": False,
            "reason": "解決窓 (48h) 内で吸収できない延長量です",
            "shifts": shift_rows,
        }
    return {
        "ok": True,
        "reason": "",
        "shifts": shift_rows,
        "new_end": prog.end_at + _td(delta_ms),
    }


def extend_program(program_id: int, delta_ms: int) -> Program:
    """押え (延長): prog.end_at を delta_ms 延ばし、後続を 48h 窓内で繰り下げる。

    - 隙間吸収: 各後続のシフト量は手前の隙間 (フィラー) で減衰する。
    - 窓内の全後続を動かしても吸収不能なら ExtendRejectedError (O1: カスケード暴走防止)。
    - EXCLUDE 制約整合のため後続は start_at 降順 (逆順) に更新し一時重複を作らない。
    - 終了 60 秒前を切った延長は拒否 (再解決伝搬前に旧イベントが発火するレースを構造的に回避)。
    """
    if delta_ms <= 0:
        raise ExtendRejectedError("延長量は正の値である必要があります")
    with transaction.atomic():
        prog = Program.objects.select_for_update().get(pk=program_id)
        now = timezone.now()
        if _ms_between(now, prog.end_at) < GUARD_MS:
            raise ExtendRejectedError("終了直前 (残り 60 秒未満) は延長できません")
        tail = list(
            Program.objects.filter(
                channel_id=prog.channel_id,
                start_at__gte=prog.end_at,
                start_at__lt=now + WINDOW,
            )
            .order_by("start_at")
            .select_for_update()
        )
        shifts, remaining = _compute_extend_shifts(prog, tail, delta_ms)
        if remaining > 0 and shifts:
            # 窓内の全後続を動かしても吸収不能 (窓境界直後との衝突は EXCLUDE が最終防壁)
            raise ExtendRejectedError("解決窓 (48h) 内で吸収できない延長量です")
        for p, shift in reversed(shifts):  # 逆順更新で EXCLUDE の一時重複を回避
            p.start_at += _td(shift)
            p.end_at += _td(shift)
            p.save(update_fields=["start_at", "end_at"])
        prog.end_at += _td(delta_ms)
        prog.save(update_fields=["end_at"])
        transaction.on_commit(lambda: resolve_channel_now.delay(prog.channel_id))
    return prog


def shorten_program(program_id: int, delta_ms: int) -> Program:
    """巻き (早終い): prog.end_at を delta_ms 縮める。後続は動かさない (再解決でフィラー充填)。

    - type=live 限定 (recorded は end_at = start_at + 素材尺 + Σbreaks の不変条件が壊れる)。
    - end_at − delta > max(start_at, now) をサーバ検証 (chk_program_time 違反/巻き戻り防止)。
    """
    if delta_ms <= 0:
        raise ShortenRejectedError("短縮量は正の値である必要があります")
    with transaction.atomic():
        prog = Program.objects.select_for_update().get(pk=program_id)
        if prog.type != ProgramType.LIVE:
            raise ShortenRejectedError("巻き (早終い) は live 番組のみ可能です")
        now = timezone.now()
        new_end = prog.end_at - _td(delta_ms)
        floor = max(prog.start_at, now)
        if new_end <= floor:
            raise ShortenRejectedError("開始時刻/現在時刻を下回る短縮はできません")
        prog.end_at = new_end
        prog.save(update_fields=["end_at"])
        transaction.on_commit(lambda: resolve_channel_now.delay(prog.channel_id))
    return prog


# --------------------------------------------------------------------------- #
#  生キューシート (LiveCue) 発火 (タイムキープ Phase 1 / #25)                    #
# --------------------------------------------------------------------------- #


class CueNotPendingError(Exception):
    """cue が pending 以外 (二重発火防止・スキップ済等) での発火/スキップ操作。"""


def _cancel_auto_fire_events(cue) -> None:
    """cue に紐づく auto_fire 予約済み PlayoutEvent (本体+バンパー) を即時 CANCELLED にする。

    タイムキープ Phase2 §2/D3: resolver の周期解決 (最大5分) に任せると、手動発火/スキップの
    直後から次の resolve までの間、手動側と自動発火予約側の両方が SCHEDULED のまま並存し
    二重発火しうる (放送事故に直結)。手動操作は状態遷移と同一トランザクション内でこれを
    即時キャンセルする。`cue.auto_fire` の真偽に関わらず常に呼んでよい (対象行が無ければ
    0件更新の no-op)。
    """
    from playout.models import PlayoutEvent, PlayoutStatus

    PlayoutEvent.objects.filter(
        channel_id=cue.rundown.program.channel_id,
        status=PlayoutStatus.SCHEDULED,
        params__live_cue_id=cue.id,
    ).update(status=PlayoutStatus.CANCELLED)


def fire_cm_now_cue(cue_id: int):
    # core→scheduling 循環回避の遅延import。EmptyCmBundleError は未捕捉のままここを
    # 素通りし呼び出し元 (core.ops_views.op_cm_now) まで伝搬する。
    from core.ops_views import fire_cm_bundle, fire_cm_dynamic
    from scheduling.models import LiveCue, LiveCueKind, LiveCueState

    with transaction.atomic():
        # cm_bundle は null 許容 FK のため select_related に含めると outer join になり、
        # Postgres は "FOR UPDATE cannot be applied to the nullable side of an outer join" で
        # 拒否する。rundown/program/channel は non-null (inner join) なので select_for_update
        # と安全に同居できるが、cm_bundle だけは外して素の FK アクセス (別クエリ) に任せる。
        cue = (
            LiveCue.objects.select_for_update()
            .select_related("rundown__program__channel")
            .get(pk=cue_id)
        )
        if cue.state != LiveCueState.PENDING:
            raise CueNotPendingError(f"cue {cue_id} は pending ではありません ({cue.state})")
        if cue.kind != LiveCueKind.CM:
            raise CueNotPendingError("この cue は CM cue ではありません")
        _cancel_auto_fire_events(cue)
        cue.state = LiveCueState.FIRING
        cue.save(update_fields=["state"])
        channel = cue.rundown.program.channel
        if cue.cm_bundle is not None:
            ev = fire_cm_bundle(channel, cue.cm_bundle, discriminator=f"live_cue:{cue.id}")
        else:
            # D3: バンドル未割付 → grid で在庫から動的充填。EmptyCmBundleError は素通しで伝搬。
            ev = fire_cm_dynamic(
                channel,
                grid=cue.grid or "15s",
                target_ms=cue.planned_duration_ms,
                discriminator=f"live_cue:{cue.id}",
            )
        cue.fired_event = ev
        cue.state = LiveCueState.AIRED
        cue.save(update_fields=["fired_event", "state"])
    return cue


def roll_vt_cue(cue_id: int):
    from core.ops_views import fire_vt_asset  # core→scheduling循環回避の遅延import
    from scheduling.models import LiveCue, LiveCueKind, LiveCueState

    with transaction.atomic():
        # asset は null 許容 FK のため select_related に含めると outer join になり、
        # fire_cm_now_cue の cm_bundle と同じ理由 (Postgres の select_for_update 制約) で外す
        # (別クエリの素の FK アクセスに任せる)。
        cue = (
            LiveCue.objects.select_for_update()
            .select_related("rundown__program__channel")
            .get(pk=cue_id)
        )
        if cue.state != LiveCueState.PENDING:
            raise CueNotPendingError(f"cue {cue_id} は pending ではありません ({cue.state})")
        if cue.kind != LiveCueKind.VT or cue.asset is None:
            raise CueNotPendingError("この cue は VT cue ではありません")
        _cancel_auto_fire_events(cue)
        cue.state = LiveCueState.FIRING
        cue.save(update_fields=["state"])
        channel = cue.rundown.program.channel
        ev = fire_vt_asset(channel, cue.asset, discriminator=f"live_cue:{cue.id}")
        cue.fired_event = ev
        cue.state = LiveCueState.AIRED
        cue.save(update_fields=["fired_event", "state"])
    return cue


def skip_live_cue(cue_id: int):
    from scheduling.models import LiveCue, LiveCueState

    with transaction.atomic():
        # fire_cm_now_cue/roll_vt_cue と同様、_cancel_auto_fire_events が使う
        # cue.rundown.program.channel_id を追加クエリ無しで引けるよう select_related する。
        cue = (
            LiveCue.objects.select_for_update()
            .select_related("rundown__program__channel")
            .get(pk=cue_id)
        )
        if cue.state != LiveCueState.PENDING:
            raise CueNotPendingError(f"cue {cue_id} は pending ではありません ({cue.state})")
        _cancel_auto_fire_events(cue)
        cue.state = LiveCueState.SKIPPED
        cue.save(update_fields=["state"])
    return cue
