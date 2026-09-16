# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""編成リゾルバ: program → playout_event 列に解決 (docs/scheduler.md)。

Celery beat が定期的に窓 [now, now+48h) を再解決する。idempotency_key は決定論的に
算出されるため、再解決は自然な diff になる。

不変条件:
- 実行中/実行済の event は不変 (status != SCHEDULED は触らない)
- 旧解決にあり今回消えた SCHEDULED event は CANCELLED に落とす (agent に tombstone 伝搬)
- 旧解決で CANCELLED だった event が今回復活した場合は SCHEDULED に戻して内容上書き
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import Channel
from medialib.models import CmCreative, CmGrid, FillerItem, NormalizeStatus
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import (
    AdBreak,
    AdBreakItem,
    ExposurePolicy,
    LiveCue,
    LiveCueAnchor,
    LiveCueKind,
    LiveCueState,
    Program,
    ProgramType,
)

logger = logging.getLogger(__name__)

# 決定論的 idempotency_key 算出のため固定値 (uuid5 namespace)。
NS_ICSTV = uuid.uuid5(uuid.NAMESPACE_DNS, "icstv.local")


@dataclass
class PendingEvent:
    """upsert 前の playout_event 表現。"""

    idempotency_key: uuid.UUID
    channel_id: int
    scheduled_at: datetime
    action: str
    asset_id: int | None = None
    live_source_id: int | None = None
    cm_bundle_id: int | None = None
    program_id: int | None = None
    ad_break_item_id: int | None = None  # #6 S10: 放確の確定逆引き経路
    params: dict[str, Any] = field(default_factory=dict)


def _ikey(channel_id: int, scheduled_at: datetime, action: str, seg: str) -> uuid.UUID:
    return uuid.uuid5(NS_ICSTV, f"{channel_id}:{scheduled_at.isoformat()}:{action}:{seg}")


def _pe(channel_id: int, at: datetime, action: str, seg: Any, **kwargs: Any) -> PendingEvent:
    return PendingEvent(
        idempotency_key=_ikey(channel_id, at, action, str(seg)),
        channel_id=channel_id,
        scheduled_at=at,
        action=action,
        **kwargs,
    )


def _ms(milliseconds: int) -> timedelta:
    return timedelta(milliseconds=milliseconds)


# ---- フィラー (隙間充填) ----


# フィラー先読みホライズン: ギャップを尺順シーケンスで埋めるが、t0 から先この範囲のみ発行する
# (全編フィラーの長大ギャップでイベントが爆発するのを防ぐ。resolve 周期ごとに前進して継ぎ足す)。
_FILLER_HORIZON = timedelta(hours=3)


def _default_filler_assets(channel: Channel) -> list:
    """channel.default_filler (FillerPlaylist) の正規化済 (r2_key あり) asset を seq 順で返す。

    未設定/item 無し/全て未正規化 なら []。これがあると filler を本編と同じ R2 prefetch 経路に
    乗せられる (agent が media_url からローカルへ先読みし LOADBG ... LOOP)。
    """
    if channel.default_filler_id is None:
        return []
    items = (
        FillerItem.objects.filter(filler_playlist_id=channel.default_filler_id)
        .select_related("asset")
        .order_by("seq")
    )
    return [it.asset for it in items if it.asset.r2_key]


def _default_filler_asset(channel: Channel):
    """default_filler playlist の先頭 (正規化済) asset。CM 不足の穴埋め等で 1 本だけ要るとき用。"""
    assets = _default_filler_assets(channel)
    return assets[0] if assets else None


def _filler_cycle(
    durs: list[int],
    anchor: datetime,
    start: datetime,
    stop: datetime,
    clamp_end: datetime,
    *,
    max_count: int,
    start_idx: int = 0,
    include_partial_head: bool = False,
    start_offset_ms: int = 0,
) -> list[tuple[int, datetime, datetime]]:
    """anchor 起点の実尺サイクルを敷き、[start, stop) に境界が入るクリップを返す純関数。

    返り: [(idx, boundary, seg_end), ...] (idx = playlist 内 0..n-1 のローテ位置)。
    - 各クリップは durs の実尺どおりに連なる (全尺→次クリップ)。
    - anchor が start より過去なら丸ごとの周回分をスキップし start 近傍から発行する (idempotent)。
    - seg_end は clamp_end でクランプ (ギャップ末尾の途中切り)。
    - max_count で打ち切り (長大ギャップのイベント爆発防止)。
    - start_idx: anchor 位置 (= ギャップ先頭) のクリップのローテ位置。実番組明けの継続ギャップで
      playlist 先頭(0)に戻さず「中断されたクリップ」から継ぐために使う (_filler_resume_index 算出)。
      先頭ギャップ (anchor=前番組 end で位相スキップ有り) では 0。
    - start_offset_ms: start_idx のクリップが中断されたクリップ内オフセット (ms)。>0 のとき、この
      最初のクリップだけ実効尺を durs[start_idx]-start_offset_ms に短縮し (= 中断位置から残り尺分
      だけ再開)、以降のクリップの境界計算にもその短縮分を伝播させる (後続クリップの境界が正しく
      前倒しされる)。0 なら従来どおり (先頭クリップも通常のフル尺)。
    - include_partial_head: True にすると boundary < start でも seg_end > start なクリップを
      (idx, start, seg_end) として先頭に追加する。EPG 投影で日またぎ途中クリップを 00:00 から
      表示するために使う (emit_filler では False のまま=境界発行に変更なし)。

    送出経路 (emit_filler フレッシュ起点) と EPG 投影 (project_filler_segments) で同一の境界式を
    共有するために切り出した。境界の振る舞いは tests/api/test_prefetch_params.py が固定する。
    """
    n = len(durs)
    total = sum(durs)
    out: list[tuple[int, datetime, datetime]] = []
    nb_off_ms = max(0, int((start - anchor) / timedelta(milliseconds=1)))
    off = (nb_off_ms // total) * total  # クリップ 0 の境界 (start 近傍以下)
    idx = start_idx
    first = True
    while len(out) < max_count:
        dur_ms = durs[idx % n] - (start_offset_ms if first else 0)
        boundary = anchor + timedelta(milliseconds=off)
        if boundary >= stop:
            break
        seg_end = min(boundary + timedelta(milliseconds=dur_ms), clamp_end)
        if boundary >= start:
            out.append((idx % n, boundary, seg_end))
        elif include_partial_head and seg_end > start:
            # gap 先頭をまたぐ途中クリップ: 開始をギャップ先端にクランプして追加。
            out.append((idx % n, start, seg_end))
        off += dur_ms
        idx += 1
        first = False
    return out


def _filler_durs(channel: Channel) -> list[int]:
    """default_filler の正規化済 asset の実尺 (ms) 列 (seq 順, 尺>0 のみ)。

    emit_filler / project_filler_segments の内部と同じフィルタで、resolve がギャップ跨ぎの
    ローテ位置を算出するためにサイクル尺だけを軽量に得る。
    """
    return [a.duration_ms for a in _default_filler_assets(channel) if (a.duration_ms or 0) > 0]


def _filler_resume_index(durs: list[int], filler_elapsed_ms: int) -> tuple[int, int]:
    """実番組で中断されたクリップとその中断位置 (index, offset_ms) を返す。

    filler_elapsed_ms = このギャップ開始までに (実番組の尺を除いて) フィラーが流れた累計実時間。
    その累計位置 r が属するクリップの index と、そのクリップ内オフセット (= r - そのクリップ
    開始までの累計尺) を返す。番組がクリップ境界ちょうどで始まった (= まだ流れていない) 場合は
    そのクリップ自身を offset=0 で返す。呼び出し側 (emit_filler) は offset>0 のとき、そのクリップ
    を残り尺 (durs[index]-offset) で再開してから通常ローテを続ける。サイクル周期で剰余を取るので
    走査は高々 n 回。

    連鎖する複数回の中断があっても、この関数の戻り値をそのまま再開に使えば filler_elapsed_ms の
    累積 (実経過時間の単純合計) は自動的に正しいクリップ内位置に一致する。これは「一度オフセット
    再開したクリップは以後その位置から実時間 1:1 で進む」という不変条件による (docs/scheduler.md)。
    """
    total = sum(durs)
    n = len(durs)
    r = filler_elapsed_ms % total
    acc = 0
    j = 0
    while acc + durs[j % n] <= r:
        acc += durs[j % n]
        j += 1
    return j % n, r - acc


# 次番組予告を番組枠内で出し始めるリード時間 (残りこの時間から終了まで表示)。
_PREVIEW_LEAD = timedelta(minutes=3)


def _next_program(channel: Channel, after: datetime) -> Program | None:
    """after 以降に始まる時系列で次の番組 (フィラーギャップを跨いでも次の「番組枠」)。

    背中合わせ番組 (次番組 start == 当番組 end) も拾うため gte。当番組自身は start < end_at
    なので after=end_at では一致しない。
    """
    return Program.objects.filter(channel=channel, start_at__gte=after).order_by("start_at").first()


def _preview_params_always(nxt: Program | None) -> dict[str, str]:
    """フィラー用: 次番組があれば常時表示の予告 params。"""
    if nxt is None:
        return {}
    return {
        "cg_preview_show": "1",
        "cg_next_title": nxt.title,
        "cg_next_start": nxt.start_at.isoformat(),
    }


def _preview_params_timed(prog_end: datetime, nxt: Program | None) -> dict[str, str]:
    """番組/生用: 次番組があれば残り3分 (end-_PREVIEW_LEAD) から表示する予告 params。"""
    if nxt is None:
        return {}
    return {
        "cg_preview_at": (prog_end - _PREVIEW_LEAD).isoformat(),
        "cg_next_title": nxt.title,
        "cg_next_start": nxt.start_at.isoformat(),
    }


def _cg_cues_param(cues: list, base_dt: datetime, clock_cues: list | tuple = ()) -> dict[str, str]:
    """GraphicCue 群 + 朝夕時計 cue を cg_cues (絶対 show_at/hide_at) の params に直す (#18 §C)。空なら {}。

    show_at/hide_at は base_dt (番組/フィラークリップ頭) + offset の絶対 ISO。clock_cues は既に絶対時刻の
    dict 列 (_clock_cue_dicts)。agent が予告タイマー機構を一般化してこの絶対時刻に show/hide する。
    """
    items = [
        {
            "layer": c.layer,
            "kind": c.kind,
            "data": c.data or {},
            "template": c.template or "",
            "show_at": (base_dt + _ms(c.show_at_ms)).isoformat(),
            "hide_at": (base_dt + _ms(c.hide_at_ms)).isoformat()
            if c.hide_at_ms is not None
            else "",
        }
        for c in cues
    ]
    items.extend(clock_cues)
    if not items:
        return {}
    return {"cg_cues": json.dumps(items, ensure_ascii=False)}


# ---- 朝・夕の左上時計 (daypart corner clock) ----

# agent amcp_planner.LAYER_CLOCK と一致させる契約 (docs/cg-layers.md のレイヤ表)。
CLOCK_LAYER = 36


def _resolve_clock_style(
    channel: Channel,
    window_style: dict | None,
    prog_override: dict | None,
) -> dict:
    """時計スタイルをチャンネル → 時間帯 → 番組/シリーズの順でマージして返す。"""
    base = channel.clock_style or {}
    return {**base, **(window_style or {}), **(prog_override or {})}


def _clock_cue_dicts(
    channel: Channel,
    near_dt: datetime,
    style_override: dict | None = None,
) -> list[dict]:
    """channel の daypart 時計窓を、絶対 show_at/hide_at の cg_cue dict 列に合成する。

    日本のTVの朝・夕の時計表示。enabled でない / 窓未設定なら []。near_dt のローカル日 (JST) と翌日に
    かけて各窓の絶対時刻を作り (= 24h 先の次窓まで agent のタイマーに必ず乗る)、show_at 昇順で返す
    (agent の _apply_cues は同一レイヤで「過ぎた窓 → CLEAR」を「現窓 → show」より前に処理する必要がある)。
    窓は end<=start を翌日跨ぎとして解釈する。style_override は番組/シリーズのスタイル上書き (None=無し)。
    各窓の style → channel.clock_style の順でマージした有効スタイルを data.style に含める。
    _cue_state のシグネチャに data が含まれるため、スタイルが変われば CG ADD が自動再発行される。
    """
    if not channel.clock_overlay_enabled:
        return []
    windows = channel.effective_clock_windows_with_style
    if not windows:
        return []
    tz = ZoneInfo(settings.TIME_ZONE)
    base_date = timezone.localtime(near_dt, tz).date()
    out: list[dict] = []
    for day_off in (0, 1):
        day = base_date + timedelta(days=day_off)
        for start_t, end_t, window_style in windows:
            effective = _resolve_clock_style(channel, window_style, style_override)
            show_at = datetime.combine(day, start_t, tzinfo=tz)
            end_day = day + timedelta(days=1) if end_t <= start_t else day
            hide_at = datetime.combine(end_day, end_t, tzinfo=tz)
            out.append(
                {
                    "layer": CLOCK_LAYER,
                    "kind": "graphic",
                    "template": "clock/corner",
                    "data": {
                        "seconds": effective.get("show_seconds", False),
                        "date": effective.get("show_date", False),
                        "style": {
                            k: v
                            for k, v in effective.items()
                            if k not in ("show_seconds", "show_date")
                        },
                    },
                    "show_at": show_at.isoformat(),
                    "hide_at": hide_at.isoformat(),
                }
            )
    out.sort(key=lambda c: c["show_at"])
    return out


# 休止中に開始する (= 送出スキップされる) 番組を読み飛ばすときに遡る最大件数。
_PREV_PROGRAM_SCAN_LIMIT = 200


def _last_program_end_before(channel: Channel, t: datetime) -> datetime | None:
    """t 以前に終わる直近 program の end_at。先頭ギャップのフィラー位相を安定させる用。

    これを使わず t0=now を位相にすると resolve 周期ごとに scheduled_at がずれ、再生中の
    フィラーが境界で奪われる (旧 _FILLER_ROTATE_SEC 由来の「5 分で途中終了」不具合)。

    broadcast_windows 有効時は「開始時刻が休止中の番組」を除外する。resolve() は同じ判定
    (Channel.is_on_air) でそれらを送出対象から外すため、実際には放送されていない番組の end_at で
    位相アンカーを前進させると、送出中フィラーが _onair_filler_anchor の探索窓 [anchor, now] から
    外れて鎖状継続が切れ、そのギャップに境界が 1 つも入らず既存の後続境界が cancel される
    (本線は最後のクリップを LOOP し続ける)。次の番組明けが「中断位置からの再開」でなく無関係な
    クリップへのカットになる実障害の原因だった。
    """
    qs = (
        Program.objects.filter(channel=channel, end_at__lte=t)
        .order_by("-end_at")
        .values_list("start_at", "end_at")
    )
    if not channel.broadcast_windows:
        row = qs.first()
        return row[1] if row else None
    for start_at, end_at in qs[:_PREV_PROGRAM_SCAN_LIMIT]:
        if channel.is_on_air(start_at):
            return end_at
    return None


def _onair_filler_anchor(
    channel: Channel, anchor: datetime, not_before: datetime
) -> PlayoutEvent | None:
    """このギャップで「今まさに送出中(=直近に時刻が来た)」のフィラー event を返す (無ければ None)。

    再 resolve はこの event の (scheduled_at + 尺) を起点に次クリップ列を継ぐ。on-air クリップは
    確定済み(immutable)なので、その「次境界」はプレイリスト編集・再正規化・total 変化に対して
    不変になり、再生中フィラーを途中で奪わない (本不具合の根治)。CANCELLED は tombstone なので除外。
    未来ギャップ (anchor > now) は [anchor, not_before] が空集合になり自然に None (=フレッシュ起点)。
    """
    return (
        PlayoutEvent.objects.filter(
            channel=channel,
            action=PlayoutAction.PLAY_FILLER,
            scheduled_at__gte=anchor,
            scheduled_at__lte=not_before,
        )
        .exclude(status=PlayoutStatus.CANCELLED)
        .select_related("asset")
        .order_by("-scheduled_at")
        .first()
    )


def _filler_phase_at(channel: Channel, durs: list[int], at: datetime) -> tuple[int, int] | None:
    """時刻 at 時点のプレイリスト位相 (index, クリップ内オフセット ms) を発行済みイベントから復元。

    at 以前で最も新しい非 CANCELLED の PLAY_FILLER を起点に、そこからの **放送中時間だけ** 位相を
    進める。放送休止中は本線へ新規 PLAY_FILLER を発行しない (= 位相が進まない) ので、休止をまたいで
    呼んでも「休止に入った瞬間の位置」がそのまま返る。休止明け (窓オープン) の再開位置はこれを使う。

    起点イベントが無ければ None (チャンネル開始直後 / 未来日投影 / テスト)。呼び出し側は従来どおり
    anchor 位相でサイクルを敷く。
    """
    total = sum(durs)
    if not durs or total <= 0:
        return None
    ev = (
        PlayoutEvent.objects.filter(
            channel=channel,
            action=PlayoutAction.PLAY_FILLER,
            scheduled_at__lte=at,
        )
        .exclude(status=PlayoutStatus.CANCELLED)
        .order_by("-scheduled_at")
        .first()
    )
    if ev is None:
        return None
    params = ev.params or {}
    p0 = params.get("pl_idx")
    if p0 is None:
        p0 = next(
            (i for i, a in enumerate(_default_filler_assets(channel)) if a.id == ev.asset_id), 0
        )
    phase = sum(durs[: int(p0) % len(durs)]) + int(params.get("in_ms", 0) or 0)
    return _filler_resume_index(durs, (phase + _on_air_ms(channel, ev.scheduled_at, at)) % total)


def emit_filler(
    channel: Channel,
    anchor: datetime,
    end: datetime,
    *,
    not_before: datetime,
    next_program: Program | None = None,
    start_idx: int = 0,
    start_offset_ms: int = 0,
) -> list[PendingEvent]:
    """フィラー playlist を各クリップの実尺どおりに並べ、クリップ境界に PLAY_FILLER を発行。

    位相の決め方:
    - 送出中フィラーがあれば、その event の (scheduled_at + 尺) を起点に次クリップ列を「鎖状」に継ぐ
      (_onair_filler_anchor)。on-air は確定済みで動かないので「次境界」も不変。これにより total が
      変わる playlist 編集・クリップ再正規化が起きても、再生中クリップを途中で奪わない。on-air 自身が
      中断→再開クリップ (params["in_ms"] 付き) の場合は、その分を差し引いた実効尺で次境界を算出する
      (フルの自然尺を使うと再開クリップの占有時間を過大評価し、次境界が後ろにずれる)。
    - 送出中フィラーが無い (チャンネル開始直後 / 未来ギャップ) ときは anchor (前番組 end_at) を起点に
      サイクルを敷く。anchor が固定なので resolve 再実行で同一 scheduled_at → 冪等。
    - start_idx: 実番組明けの継続ギャップで先頭(0)に戻さず「中断されたクリップ」から継ぐローテ起点
      (resolve が _filler_resume_index で算出して渡す)。フレッシュ起点のみ反映。先頭ギャップは 0。
      鎖状継続パス (on-air あり) は on-air の pl_idx を真とし start_idx を無視する。
    - start_offset_ms: start_idx のクリップが中断された位置 (ms)。>0 なら先頭クリップを中断位置
      から残り尺分だけ再開し (params["in_ms"]/["out_ms"] を送出ノードへ渡し AMCP SEEK/LENGTH に使う)、
      後続クリップの境界もその短縮分だけ前倒しになる (_filler_cycle に委譲)。
    - 発行範囲は [max(not_before, anchor), min(end, not_before+_FILLER_HORIZON))。長大ギャップは
      周期再 resolve で継ぎ足す。下限を anchor で切り上げるのが放送休止中に発行しないための担保で、
      呼び出し側 (resolve/_filler_seg_phase) が休止をまたぐ区間に「窓オープン時刻」を anchor として
      渡す。24h 運用や同一窓内の継続では anchor <= not_before なので従来と同じ下限になる。
    - idempotency_key は scheduled_at のみに依存 (seg=0)。境界時刻さえ再現すれば差し替えにならず update 扱い。
    - 各クリップ loop=True (境界ジッタの黒落ち防止)。次境界で差し替わるため事実上ループせず全尺→次クリップ。
    - フォールバック (default_filler 未設定/未正規化): 従来どおり anchor で単発ループ (尺不明のため)。
    """
    # CG: フィラー中は Lバー(ロゴ+時計, タイトル空)常時 + 次番組予告(あれば)常時。
    cg = {"cg_title": "", "cg_channel": channel.name, **_preview_params_always(next_program)}
    # フィラー基本グラフィックセット (Channel context=filler)。各クリップ境界を基準に絶対時刻化。
    filler_cues = list(channel.graphic_cues.filter(context="filler"))
    assets = [a for a in _default_filler_assets(channel) if (a.duration_ms or 0) > 0]
    if assets:
        durs = [a.duration_ms for a in assets]
        n = len(assets)
        horizon_end = min(end, not_before + _FILLER_HORIZON)
        # 発行下限は anchor より前に戻さない。放送休止をまたぐ区間では呼び出し側が anchor に
        # 「窓オープン時刻」を渡すため、これが休止中への発行を止める防波堤になる (案B の要)。
        # 24h 運用や同一窓内の継続では anchor <= not_before なので従来と同一の下限になる。
        emit_start = max(not_before, anchor)

        def _emit_at(boundary: datetime, pidx: int, in_ms: int = 0) -> PendingEvent:
            asset = assets[pidx % n]
            natural_dur = durs[pidx % n]
            dur = natural_dur - in_ms
            seg_end = min(boundary + timedelta(milliseconds=dur), end)
            params = {
                "filler_playlist_id": channel.default_filler_id,
                "clip": f"filler/{asset.id}",
                "loop": True,
                "until": seg_end.isoformat(),
                "r2_key": asset.r2_key,
                "pl_idx": pidx % n,  # 鎖状継続のためにローテ位置を記録
                **cg,
                **_cg_cues_param(filler_cues, boundary, _clock_cue_dicts(channel, boundary)),
            }
            if in_ms:
                # 中断→再開クリップ: agent が AMCP SEEK/LENGTH を組むための区間情報。
                params["in_ms"] = in_ms
                params["out_ms"] = natural_dur
            return _pe(
                channel.id,
                boundary,
                PlayoutAction.PLAY_FILLER,
                0,
                asset_id=asset.id,
                params=params,
            )

        events: list[PendingEvent] = []
        onair = _onair_filler_anchor(channel, anchor, not_before)
        if onair is not None:
            # 鎖状継続: on-air クリップ (= 確定済み) の (scheduled_at + 実効尺) を次クリップの境界にする。
            # pl_idx が無い旧 event は asset_id からローテ位置を逆引き (見つからなければ 0)。
            p0 = onair.params.get("pl_idx")
            if p0 is None:
                p0 = next((i for i, a in enumerate(assets) if a.id == onair.asset_id), 0)
            p0 = int(p0) % n
            onair_natural_dur = (onair.asset.duration_ms if onair.asset else None) or durs[p0]
            onair_in_ms = int((onair.params or {}).get("in_ms", 0) or 0)
            onair_dur = onair_natural_dur - onair_in_ms
            boundary = onair.scheduled_at + timedelta(milliseconds=onair_dur)
            p = p0 + 1
            while len(events) < 512 and boundary < horizon_end:
                if boundary >= emit_start:
                    events.append(_emit_at(boundary, p))
                boundary += timedelta(milliseconds=durs[p % n])
                p += 1
            return events
        # フレッシュ起点: anchor 固定の実尺サイクルで [emit_start, horizon_end) を埋める。
        # 境界の算出は _filler_cycle に集約 (EPG 投影 project_filler_segments と同一式を共有)。
        # start_idx/start_offset_ms で先頭ギャップは 0、実番組明けは「中断されたクリップ」から起こす。
        for pos, (i, boundary, _seg_end) in enumerate(
            _filler_cycle(
                durs,
                anchor,
                emit_start,
                horizon_end,
                end,
                max_count=512,
                start_idx=start_idx,
                start_offset_ms=start_offset_ms,
            )
        ):
            events.append(_emit_at(boundary, i, in_ms=start_offset_ms if pos == 0 else 0))
        return events
    # フォールバック: 既定フィラー未設定/未正規化 → ノードローカル filler/<id> (無ければ agent slate)
    filler_id = channel.default_filler_id or "default"
    return [
        _pe(
            channel.id,
            anchor,
            PlayoutAction.PLAY_FILLER,
            0,
            params={
                "filler_playlist_id": channel.default_filler_id,
                "clip": f"filler/{filler_id}",
                "loop": True,
                "until": end.isoformat(),
                **cg,
                **_cg_cues_param([], anchor, _clock_cue_dicts(channel, anchor)),
            },
        )
    ]


@dataclass
class RerunSegment:
    """フィラー送出区間のうち「再放送」として視聴者向け面に出すもの (表示専用)。

    core.epg._build_block / day_list が Program と同じ duck-type で扱えるよう、id/title/
    start_at/end_at/resolved_genre を持つ。id は素材 id だが番組詳細リンクは張らない (合成行)。
    """

    id: int
    title: str
    start_at: datetime
    end_at: datetime
    resolved_genre: str = ""


def _event_until(event: PlayoutEvent, fallback_ms: int) -> datetime:
    """発行済みフィラー event の送出終端 (params["until"]、無ければ scheduled_at + 尺)。"""
    raw = (event.params or {}).get("until") if event.params else None
    parsed = parse_datetime(raw) if raw else None
    return parsed or (event.scheduled_at + timedelta(milliseconds=fallback_ms))


def _project_chain(
    channel: Channel,
    assets: list,
    durs: list[int],
    start: datetime,
    t1: datetime,
    start_idx: int,
    segs: list[RerunSegment],
    max_per_gap: int,
) -> None:
    """start..t1 をフィラークリップで鎖状に外挿。未来 program を挟む場合、番組明けは中断された
    クリップの中断位置 (オフセット) から再開する (_filler_cycle/_filler_resume_index と同じ規約。
    emit_filler フレッシュ起点・project_filler_segments の純計算フォールバックと外挿結果を揃える)。
    rerun_eligible のみ segs に追加。発行ホライズン (now+3h) より先のテールを実発行イベントの末尾
    から決定論的に継ぐ用途 (DB 書込み無し)。"""
    n = len(assets)
    cursor = start
    programs = list(
        Program.objects.filter(channel=channel, end_at__gt=start, start_at__lt=t1).order_by(
            "start_at"
        )
    )

    total = sum(durs)

    def _fill(gap_start: datetime, gap_end: datetime, idx: int, offset_ms: int) -> None:
        # 放送休止中はフィラーを流さない (= 位相も進まない) ので、放送中区間ごとに区間先頭を
        # anchor にサイクルを敷き直し、区間の実時間だけ位相を進める (送出経路と同じ規約)。
        for sub_s, sub_e in _on_air_subsegments(channel, gap_start, gap_end):
            for i, boundary, seg_end in _filler_cycle(
                durs,
                sub_s,
                sub_s,
                sub_e,
                sub_e,
                max_count=max_per_gap,
                start_idx=idx,
                start_offset_ms=offset_ms,
            ):
                a = assets[i]
                if a.rerun_eligible:
                    segs.append(
                        RerunSegment(id=a.id, title=a.title, start_at=boundary, end_at=seg_end)
                    )
            idx, offset_ms = _filler_resume_index(
                durs,
                (sum(durs[:idx]) + offset_ms + int((sub_e - sub_s) / timedelta(milliseconds=1)))
                % total,
            )

    idx, offset_ms = start_idx % n, 0
    filler_elapsed_ms = sum(durs[: start_idx % n])
    for prog in programs:
        if prog.start_at > cursor:
            _fill(cursor, prog.start_at, idx, offset_ms)
            filler_elapsed_ms += _on_air_ms(channel, cursor, prog.start_at)
            idx, offset_ms = _filler_resume_index(durs, filler_elapsed_ms)
        cursor = max(cursor, prog.end_at)
    if cursor < t1:
        _fill(cursor, t1, idx, offset_ms)


def project_filler_segments(
    channel: Channel, t0: datetime, t1: datetime, *, max_per_gap: int = 512
) -> list[RerunSegment]:
    """[t0, t1) のフィラー区間を投影し、rerun_eligible 素材のみ表示セグメントで返す。

    番組表 (EPG) / 本日の番組リストで「編成に無いフィラー帯」を再放送として埋める表示専用投影。
    実放送と一致させるため二段構え:

      1. 発行済み PLAY_FILLER イベント (= 送出ノードが実際に消費する真の並び) が窓に掛かる範囲は
         そのイベントの asset/区間をそのまま採用する。鎖状継続 (_onair_filler_anchor) や番組明けの
         resume_index の結果がそのまま反映されるので「番組表 ≠ 実放送」のズレが原理的に起きない。
      2. 発行ホライズン (emit は now+_FILLER_HORIZON までしか敷かない) より先のテールは、最後の
         発行イベントの pl_idx を起点に _project_chain で決定論的に外挿する。

    窓に発行イベントが 1 件も掛からないとき (未来日の番組表 / テスト) は従来どおり anchor +
    _filler_cycle の純計算で投影する (送出経路 emit_filler のフレッシュ起点と同じ境界式)。DB 書込み無し。
    rerun_eligible でないクリップ (局ID/プロモ等) は境界計算には含めるがセグメントは出さない (空欄)。
    """
    assets = [a for a in _default_filler_assets(channel) if (a.duration_ms or 0) > 0]
    if not assets:
        return []
    durs = [a.duration_ms for a in assets]
    n = len(assets)
    segs: list[RerunSegment] = []

    # --- 1) 発行済みフィラーイベントを「真」として読む (窓に掛かる範囲) ---
    events = (
        PlayoutEvent.objects.filter(
            channel=channel,
            action=PlayoutAction.PLAY_FILLER,
            scheduled_at__lt=t1,
        )
        .exclude(status=PlayoutStatus.CANCELLED)
        .select_related("asset")
        .order_by("scheduled_at")
    )
    overlapping = False
    seed_idx: int | None = None
    seed_until: datetime | None = None
    for e in events:
        a = e.asset
        if a is None:
            continue
        until = _event_until(e, a.duration_ms or 0)
        if until <= t0 or e.scheduled_at >= t1:
            continue  # 窓外
        overlapping = True
        pl = (e.params or {}).get("pl_idx")
        if pl is None:
            pl = next((i for i, x in enumerate(assets) if x.id == a.id), None)
        if pl is not None:
            seed_idx, seed_until = int(pl) % n, until
        # 放送休止 (broadcast_windows 窓外) の時間帯には実際にはフィラーでなく PLAY_SLATE が流れる。
        # 発行済みイベント経路でも on-air のもののみ再放送帯として投影する (純計算/外挿経路と揃える)。
        if a.rerun_eligible and channel.is_on_air(e.scheduled_at):
            segs.append(RerunSegment(id=a.id, title=a.title, start_at=e.scheduled_at, end_at=until))

    if overlapping:
        # 2) テール: 最後の発行イベントの末尾から次クリップを鎖状に外挿。
        if seed_idx is not None and seed_until is not None and seed_until < t1:
            _project_chain(
                channel, assets, durs, seed_until, t1, (seed_idx + 1) % n, segs, max_per_gap
            )
        return segs

    # --- 発行イベント無し: 従来の純計算投影 (未来日 / テスト) ---
    programs = list(
        Program.objects.filter(channel=channel, end_at__gt=t0, start_at__lt=t1).order_by("start_at")
    )
    prev_end = _last_program_end_before(channel, t0)

    total = sum(durs)

    def _fill(
        anchor: datetime,
        gap_start: datetime,
        gap_end: datetime,
        start_idx: int,
        start_offset_ms: int,
        *,
        leading: bool,
    ) -> None:
        # 放送休止中はフィラーを流さない (= 位相も進まない)。放送中区間ごとにサイクルを敷き直し、
        # 区間の実時間だけ位相を進める (送出経路 resolve/_filler_seg_phase と同じ規約)。
        idx, offset_ms = start_idx, start_offset_ms
        first = True
        for sub_s, sub_e in _on_air_subsegments(channel, gap_start, gap_end):
            sub_anchor = (
                _filler_seg_anchor(channel, sub_s, anchor, leading=leading, prev_end=prev_end)
                if first
                else sub_s
            )
            first = False
            for i, boundary, seg_end in _filler_cycle(
                durs,
                sub_anchor,
                sub_s,
                sub_e,
                sub_e,
                max_count=max_per_gap,
                start_idx=idx,
                include_partial_head=True,  # 日またぎ途中クリップを区間先頭にクランプして表示
                start_offset_ms=offset_ms,
            ):
                a = assets[i]
                if a.rerun_eligible:
                    segs.append(
                        RerunSegment(id=a.id, title=a.title, start_at=boundary, end_at=seg_end)
                    )
            idx, offset_ms = _filler_resume_index(
                durs,
                (sum(durs[:idx]) + offset_ms + int((sub_e - sub_s) / timedelta(milliseconds=1)))
                % total,
            )

    # 送出経路と同じく実番組をローテから差し引いた累計実時間で「中断位置」を継ぐ (先頭ギャップは位相)。
    filler_elapsed_ms = 0
    cursor = t0
    leading = True
    for prog in programs:
        anchor = prev_end if (leading and prev_end is not None) else cursor
        start_idx, start_offset_ms = (
            (0, 0) if leading else _filler_resume_index(durs, filler_elapsed_ms)
        )
        if prog.start_at > cursor:
            _fill(anchor, cursor, prog.start_at, start_idx, start_offset_ms, leading=leading)
        if prog.start_at > anchor:
            filler_elapsed_ms += _on_air_ms(channel, anchor, prog.start_at)
        cursor = prog.end_at
        leading = False
    if cursor < t1:
        anchor = prev_end if (leading and prev_end is not None) else cursor
        start_idx, start_offset_ms = (
            (0, 0) if leading else _filler_resume_index(durs, filler_elapsed_ms)
        )
        _fill(anchor, cursor, t1, start_idx, start_offset_ms, leading=leading)
    return segs


# ---- 生番組 ----

# CM cue の layer20 バンパー (CM IN/OUT)。core.ops_views.BUMPER_LAYER と値を一致させること。
# core.ops_views は scheduling.services → scheduling.tasks → scheduling.resolver を辿ってこの
# モジュールに戻ってくる import 経路を持つため、ここで core.ops_views を直接 import すると循環
# import になる (docs/timekeeper-live.md Phase2 計画 §1 で確認済み)。値の複製で回避する。
_BUMPER_LAYER = 20

# exposure_policy (#27、docs/site-only-broadcast.md): YTミラー2本の seg 識別子。
# _ikey の seg 引数として使い、同時刻・同actionの PLAY_ASSET 等と衝突しない別イベント系列にする。
YT_PUBLIC_MIRROR_SEG = "yt_public"
YT_MEMBERS_MIRROR_SEG = "yt_members"


def _exposure_mirror_needs(policy: str) -> tuple[bool, bool]:
    """(公開ミラーをfiller化する必要があるか, メンバーミラーをroute化する必要があるか)。

    public 以外は常に公開ミラーをフィラー化 (軸①=旧 site_only の実体)。members_yt_site のみ
    メンバーミラーを本編化 (軸③)。site_members/site_public は公開ミラーのみ、メンバーミラーは
    既定(filler=待機画)のまま変化しない。
    """
    needs_public_filler = policy != ExposurePolicy.PUBLIC
    needs_members_route = policy == ExposurePolicy.MEMBERS_YT_SITE
    return needs_public_filler, needs_members_route


def emit_exposure_mirror(
    prog: Program, *, prev_prog: Program | None, next_prog: Program | None
) -> list[PendingEvent]:
    """exposure_policy に応じた YT ミラー制御イベント (#27、docs/site-only-broadcast.md §4.3)。

    本線イベント (emit_recorded/emit_live) は変えず、ミラー制御イベントを追加するだけ。
    同一 seg で背中合わせ(境界が連続)かつ必要性が同じ番組は 1 つの連続区間として扱い、
    区間の先頭/末尾でだけ発行する。境界ごとに毎回 ROUTE(end)/FILLER(start) を出すと同時刻の
    2 イベントが due_for_take の不定順で処理され本編が一瞬露出しうるため (フリッカ防止)。

    exposure_policy 変更時の掃除は不要: 新しい policy で不要になったイベントは今回 pending に
    含まれなくなるだけで、_commit() の「今回無い既存 SCHEDULED は CANCELLED」ロジックが
    自動的に tombstone 化する (idempotency_key は params でなく action/seg で決まるため)。

    fc_required_level (ファンクラブ ティア限定、#27 Phase B・docs/fanclub.md §6.4) が設定された
    番組も公開ミラーをフィラー化する (needs_public に OR)。site-member 軸と違い「YouTube メンバー
    限定へ本編を出す」概念はファンクラブ ティアには無いため needs_members には影響しない。
    これが無いと exposure_policy=public のまま fc_required_level だけでゲートした番組が、
    サイトでは正しく隠れつつ公開 YouTube 本線ではそのまま素通しになる
    (#27 Phase B レビューで発見: サイト側ゲートと YT ミラー側ゲートの不整合)。
    """
    policy = prog.resolved_exposure_policy
    needs_public, needs_members = _exposure_mirror_needs(policy)
    needs_public = needs_public or prog.fc_required_level is not None
    prev_public, prev_members = (
        _exposure_mirror_needs(prev_prog.resolved_exposure_policy) if prev_prog else (False, False)
    )
    if prev_prog is not None:
        prev_public = prev_public or prev_prog.fc_required_level is not None
    next_public, next_members = (
        _exposure_mirror_needs(next_prog.resolved_exposure_policy) if next_prog else (False, False)
    )
    if next_prog is not None:
        next_public = next_public or next_prog.fc_required_level is not None
    prev_adjacent = prev_prog is not None and prev_prog.end_at == prog.start_at
    next_adjacent = next_prog is not None and prog.end_at == next_prog.start_at

    events: list[PendingEvent] = []

    def _seg_events(
        needs: bool,
        prev_needs: bool,
        next_needs: bool,
        seg: str,
        open_action: str,
        close_action: str,
    ) -> None:
        if not needs:
            return
        if not (prev_adjacent and prev_needs):
            events.append(
                _pe(
                    prog.channel_id,
                    prog.start_at,
                    open_action,
                    seg,
                    program_id=prog.id,
                    params={"mirror_seg": seg},
                )
            )
        if not (next_adjacent and next_needs):
            events.append(
                _pe(
                    prog.channel_id,
                    prog.end_at,
                    close_action,
                    seg,
                    program_id=prog.id,
                    params={"mirror_seg": seg},
                )
            )

    _seg_events(
        needs_public,
        prev_public,
        next_public,
        YT_PUBLIC_MIRROR_SEG,
        PlayoutAction.YT_MIRROR_FILLER,
        PlayoutAction.YT_MIRROR_ROUTE,
    )
    _seg_events(
        needs_members,
        prev_members,
        next_members,
        YT_MEMBERS_MIRROR_SEG,
        PlayoutAction.YT_MIRROR_ROUTE,
        PlayoutAction.YT_MIRROR_FILLER,
    )
    return events


def emit_live(prog: Program) -> list[PendingEvent]:
    params: dict[str, Any] = {
        # CG: 生番組中も Lバー(番組名)常設 + 残り3分から次番組予告 + 自動グラフィックセット。
        "cg_title": prog.title,
        "cg_channel": prog.channel.name,
        **_preview_params_timed(prog.end_at, _next_program(prog.channel, prog.end_at)),
        **_cg_cues_param(
            list(prog.graphic_cues.all()),
            prog.start_at,
            []
            if prog.resolved_clock_hidden
            else _clock_cue_dicts(prog.channel, prog.start_at, prog.resolved_clock_style_override),
        ),
    }
    if prog.resolved_lbar_hidden:
        params["cg_lbar_hidden"] = "1"
    if prog.cm_bundle_id:
        params["cm_bundle_id"] = prog.cm_bundle_id
    if prog.live_source is not None:
        # agent 側で rtmp://<MediaMTX>:1935/<app>/<key> を組み立てる
        params["rtmp_app"] = prog.live_source.rtmp_app
        params["rtmp_key"] = prog.live_source.rtmp_key
    if prog.record_live:
        # PlayoutEvent には録画専用フィールドが無いため params 経由で agent に伝える。
        # program_id は ingest キー (ingest/live_recording/<ch>/<program_id>.mp4) の組み立てに使う。
        params["record"] = "1"
        params["program_id"] = str(prog.id)
    events = [
        _pe(
            prog.channel_id,
            prog.start_at,
            PlayoutAction.CUT_LIVE,
            0,
            live_source_id=prog.live_source_id,
            cm_bundle_id=prog.cm_bundle_id,
            program_id=prog.id,
            params=params,
        )
    ]
    events.extend(_emit_live_auto_fire_cues(prog, params))
    return events


def _auto_fire_at(prog: Program, cue: LiveCue) -> datetime:
    """auto_fire cue の予約絶対時刻を求める (タイムキープ Phase3 D1 / §11)。

    - anchor=wallclock: 番組日の固定時刻 auto_wall_time に発火。押え/巻きによる開始ズレに
      左右されない (ネット CM の定時ジョイン等の固定時刻 cue 向け)。番組が日跨ぎで、素直に
      組むと開始前になる場合は翌日へ送る。
    - anchor=start (既定): 従来どおり prog.start_at + auto_offset_ms。
    OverflowError は呼び出し側が cue 単位でスキップする (巨大 auto_offset_ms 防御)。
    """
    if cue.auto_anchor == LiveCueAnchor.WALLCLOCK and cue.auto_wall_time is not None:
        tz = timezone.get_current_timezone()
        local_date = timezone.localtime(prog.start_at, tz).date()
        at = timezone.make_aware(datetime.combine(local_date, cue.auto_wall_time), tz)
        if at < prog.start_at:
            at += timedelta(days=1)  # 日跨ぎ番組で壁時計が翌日側のケース
        return at
    return prog.start_at + timedelta(milliseconds=cue.auto_offset_ms or 0)


def _emit_live_auto_fire_cues(prog: Program, cut_live_params: dict[str, Any]) -> list[PendingEvent]:
    """`prog.live_rundown` の auto_fire cue を絶対時刻の PendingEvent として予約発行する
    (タイムキープ Phase2 §1)。

    `state=LiveCueState.PENDING` の絞り込みは必須 (D1): これが無いと手動発火/スキップ済みの
    cue まで次の resolve (最大5分周期) で拾い直してしまい、二重発火の原因になる (`_commit` の
    revived 分岐に引っかかる)。
    """
    rundown = getattr(prog, "live_rundown", None)  # OneToOne逆参照。無ければ None (既存idiom踏襲)
    if rundown is None:
        return []
    events: list[PendingEvent] = []
    cues = (
        rundown.cues.filter(auto_fire=True, state=LiveCueState.PENDING)
        .select_related("cm_bundle", "asset")
        .order_by("seq")
    )
    for cue in cues:
        try:
            at = _auto_fire_at(prog, cue)
        except OverflowError:
            # auto_offset_ms は studio 側で妥当性検証済みだが (edit_views._validate_auto_offset)、
            # 既存データや検証を経ない経路からの異常値で resolve() 全体 (このチャンネルの
            # 全番組) が壊れないよう、この cue だけを静かにスキップする防御 (2026-07-04 レビューで
            # 巨大な auto_offset_ms が OverflowError を起こし channel 丸ごとの resolve が止まる
            # ことを実証)。
            logger.warning(
                "auto_fire cue=%s の auto_offset_ms=%s が不正なためスキップ",
                cue.id,
                cue.auto_offset_ms,
            )
            continue
        # cg_* (Lバー/予告/自動グラフィックセット等) は emit_live が既に確定済みの値をそのまま
        # コピーする。resolve は未来の予約なので「直近 cut_live から引き継ぐ」概念自体が無い
        # (D6: fire_cm_bundle/fire_vt_asset の _last_live 方式とは異なる)。
        cue_params: dict[str, Any] = {
            k: v for k, v in cut_live_params.items() if k.startswith("cg_")
        }
        cue_params["live_cue_id"] = cue.id  # D2: params__live_cue_id=cue.id で後から逆引きする鍵
        if prog.live_source is not None and cut_live_params.get("rtmp_app"):
            cue_params["return_rtmp_app"] = cut_live_params["rtmp_app"]
            cue_params["return_rtmp_key"] = cut_live_params["rtmp_key"]

        if cue.kind == LiveCueKind.CM and cue.cm_bundle_id:
            items = list(cue.cm_bundle.items.select_related("cm_asset__asset").order_by("seq"))
            if not items:
                # core.ops_views.fire_cm_bundle は EmptyCmBundleError を送出するが、ここは
                # request/response の無い周期バッチなので例外で resolve() 全体を壊さず、この
                # cue の発行だけを静かにスキップする (ログにのみ残す)。
                logger.warning(
                    "auto_fire cue=%s の CM バンドル(cm_bundle_id=%s)が空のためスキップ",
                    cue.id,
                    cue.cm_bundle_id,
                )
                continue
            clips = ",".join(
                f"cm/{it.cm_asset_id}:{it.cm_asset.asset.duration_ms or 0}" for it in items
            )
            bundle_total_ms = sum(it.cm_asset.asset.duration_ms or 0 for it in items)
            events.append(
                _pe(
                    prog.channel_id,
                    at,
                    PlayoutAction.PLAY_CM_BUNDLE,
                    f"live_cue:{cue.id}",
                    cm_bundle_id=cue.cm_bundle_id,
                    program_id=prog.id,
                    params={**cue_params, "clips": clips, "bundle_id": cue.cm_bundle_id},
                )
            )
            events.append(
                _pe(
                    prog.channel_id,
                    at,
                    PlayoutAction.OVERLAY_OP,
                    f"live_cue_bumper_in:{cue.id}",
                    program_id=prog.id,
                    params={
                        "overlay_layer": str(_BUMPER_LAYER),
                        "overlay_op": "show",
                        "overlay_kind": "graphic",
                        "overlay_template": "bumper/cm-in",
                        "overlay_data": "{}",
                        "live_cue_id": cue.id,
                    },
                )
            )
            events.append(
                _pe(
                    prog.channel_id,
                    at + timedelta(milliseconds=bundle_total_ms),
                    PlayoutAction.OVERLAY_OP,
                    f"live_cue_bumper_out:{cue.id}",
                    program_id=prog.id,
                    params={
                        "overlay_layer": str(_BUMPER_LAYER),
                        "overlay_op": "hide",
                        "overlay_kind": "graphic",
                        "live_cue_id": cue.id,
                    },
                )
            )
        elif cue.kind == LiveCueKind.VT and cue.asset_id:
            asset = cue.asset
            vt_params: dict[str, Any] = {
                **cue_params,
                "clip": f"asset/{asset.id}",
                "in_ms": 0,
                "out_ms": asset.duration_ms or 0,
            }
            if asset.r2_key:
                vt_params["r2_key"] = asset.r2_key
            events.append(
                _pe(
                    prog.channel_id,
                    at,
                    PlayoutAction.PLAY_VT,
                    f"live_cue:{cue.id}",
                    asset_id=cue.asset_id,
                    program_id=prog.id,
                    params=vt_params,
                )
            )
        elif cue.kind == LiveCueKind.CM:  # bundle 未割付 = grid 動的充填 CM (D3)
            # 動的充填は発火時点の在庫に依存するため、resolve (最大数週間先の予約発行) では
            # 確定できない。auto_fire を予約せず警告し、手動発火 (op_cm_now) に委ねる
            # (cue は pending のまま残り、残CM 台帳に出続けて手動対応を促す)。
            logger.warning(
                "auto_fire cue=%s は動的充填CM(バンドル未割付)のため予約不可 → 手動発火が必要",
                cue.id,
            )
    return events


# ---- 録画番組 (本編 ⊕ CM枠) ----


def emit_recorded(prog: Program, provider: Any = None) -> list[PendingEvent]:
    asset = prog.asset
    if asset is None or asset.duration_ms is None:
        raise ValueError(
            f"recorded program {prog.id} '{prog.title}' has no asset or duration",
        )

    events: list[PendingEvent] = []
    breaks = list(prog.ad_breaks.order_by("offset_ms"))
    head_ms = 0
    air = prog.start_at
    seg = 0

    for br in breaks:
        # 本編セグメント [head, br.offset_ms)
        events.append(
            _pe(
                prog.channel_id,
                air,
                PlayoutAction.PLAY_ASSET,
                seg,
                asset_id=asset.id,
                program_id=prog.id,
                params={
                    "clip": f"asset/{asset.id}",
                    "in_ms": head_ms,
                    "out_ms": br.offset_ms,
                    # prefetch 用 mezzanine R2 キー (gRPC 配信時に presigned URL へ。overview 3.2)
                    **({"r2_key": asset.r2_key} if asset.r2_key else {}),
                },
            )
        )
        air += _ms(br.offset_ms - head_ms)
        head_ms = br.offset_ms
        seg += 1

        # CM 枠。air = 枠開始の実放送時刻 (air_at: 線引き/タイムランク判定の基準。#6)
        pairs = fill_break(br, air, provider=provider)
        cm_total_ms = 0
        for i, (item, cm) in enumerate(pairs):
            cm_dur = cm.asset.duration_ms or 0
            events.append(
                _pe(
                    prog.channel_id,
                    air,
                    PlayoutAction.PLAY_CM,
                    f"{seg}.{i}",
                    asset_id=cm.asset_id,
                    program_id=prog.id,
                    ad_break_item_id=item.id,  # S10: 放確の確定逆引き
                    params={
                        "clip": f"cm/{cm.asset_id}",
                        "advertiser": cm.advertiser,
                        "duration_ms": cm_dur,
                        **({"r2_key": cm.asset.r2_key} if cm.asset.r2_key else {}),
                    },
                )
            )
            air += _ms(cm_dur)
            cm_total_ms += cm_dur

        # 在庫不足: 枠の残尺はフィラーで埋めて air 進行を維持 (検算崩れ防止)
        deficit_ms = br.duration_ms - cm_total_ms
        if deficit_ms > 0:
            logger.warning(
                "CM 在庫不足 break=%s grid=%s deficit=%dms",
                br.id,
                br.grid,
                deficit_ms,
            )
            fill_asset = _default_filler_asset(prog.channel)
            fill_id = fill_asset.id if fill_asset else (prog.channel.default_filler_id or "default")
            fill_params: dict[str, Any] = {
                "filler_playlist_id": prog.channel.default_filler_id,
                "clip": f"filler/{fill_id}",
                "loop": False,
                "duration_ms": deficit_ms,
                "reason": "cm_insufficient",
            }
            if fill_asset is not None:
                fill_params["r2_key"] = fill_asset.r2_key  # 本編同様 prefetch 経路に乗せる
            events.append(
                _pe(
                    prog.channel_id,
                    air,
                    PlayoutAction.PLAY_FILLER,
                    f"{seg}.fill",
                    program_id=prog.id,
                    asset_id=fill_asset.id if fill_asset else None,
                    params=fill_params,
                )
            )
            air += _ms(deficit_ms)
        seg += 1

    # 最終本編セグメント [head, asset.duration_ms]
    events.append(
        _pe(
            prog.channel_id,
            air,
            PlayoutAction.PLAY_ASSET,
            seg,
            asset_id=asset.id,
            program_id=prog.id,
            params={
                "clip": f"asset/{asset.id}",
                "in_ms": head_ms,
                "out_ms": asset.duration_ms,
                **({"r2_key": asset.r2_key} if asset.r2_key else {}),
            },
        )
    )

    expected_end = air + _ms(asset.duration_ms - head_ms)
    if expected_end != prog.end_at:
        logger.warning(
            "emit_recorded sanity: prog=%s expected_end=%s != end_at=%s",
            prog.id,
            expected_end,
            prog.end_at,
        )

    _annotate_cg(events, prog, provider)
    return events


def _annotate_cg(events: list[PendingEvent], prog: Program, provider: Any) -> None:
    """as-run 計画に CG (Lバー/番組タイトル/CM バンパー/提供) の派生ヒントを載せる (casparcg.md §3.4)。

    agent は params の cg_* フラグを読んで CG 1-20/30/50 を ADD/PLAY/STOP する (本線とは別 layer)。
    - 番組頭の本編 = Lバー(1-30)常設 ADD + バンパー(1-20)背面ロード + 提供(1-50)
    - CM 先頭 = バンパー PLAY + Lバー STOP (退避)
    - CM 明けの本編 = Lバー PLAY + バンパー STOP (復帰)
    """
    credit_fn = getattr(provider, "program_credit", None)
    credit = credit_fn(prog, prog.start_at) if credit_fn is not None else None
    # Lバーのタイトル/予告は全本編セグメントに載せる (CM 明け segment でも title を保つ + 残り3分
    # 予告タイマーを再確立できるように)。OverlayManager が ADD/UPDATE/タイマーを調停する。
    base_cg = {
        "cg_title": prog.title,
        "cg_channel": prog.channel.name,
        **_preview_params_timed(prog.end_at, _next_program(prog.channel, prog.end_at)),
        # 番組の自動グラフィックセット (絶対時刻) + 朝夕時計。全本編 segment に載せ CM 跨ぎでも再確立できる。
        **_cg_cues_param(
            list(prog.graphic_cues.all()),
            prog.start_at,
            []
            if prog.resolved_clock_hidden
            else _clock_cue_dicts(prog.channel, prog.start_at, prog.resolved_clock_style_override),
        ),
    }
    if prog.resolved_lbar_hidden:
        base_cg["cg_lbar_hidden"] = "1"
    first_asset = True
    prev_action = None
    for ev in events:
        if ev.action == PlayoutAction.PLAY_ASSET:
            ev.params = {**ev.params, **base_cg}
            if first_asset:  # 番組頭: バンパー背面ロード (+ 提供)
                ev.params["cg_lbar_add"] = "1"
                if credit:
                    ev.params["cg_sponsor"] = credit["text"]
                    ev.params["cg_template"] = credit["template"]
                first_asset = False
            else:  # CM 明けの本編復帰
                ev.params["cg_cm_out"] = "1"
        elif ev.action == PlayoutAction.PLAY_CM and prev_action == PlayoutAction.PLAY_ASSET:
            ev.params = {**ev.params, "cg_cm_in": "1"}  # 枠先頭 = CM IN
        prev_action = ev.action


# ---- CM 充填 (15/20 グリッド・均等ローテ) ----


def _unit_ms(grid: str) -> int:
    return 15000 if grid == CmGrid.G15 else 20000


def _free_pool_qs_for(grid: str, max_dur_ms: int, today):
    """契約外フリー素材プール (grid/枠尺を直接受ける版・AdBreak 非依存)。

    grid 一致・READY・campaign 期間内・max_airings 未達。尺は枠尺以内 (残尺ベース選定はループ側)。
    """
    return (
        CmCreative.objects.select_related("asset")
        .filter(grid=grid, asset__normalize_status=NormalizeStatus.READY)
        .filter(asset__duration_ms__gt=0, asset__duration_ms__lte=max_dur_ms)
        .filter(Q(campaign_start__isnull=True) | Q(campaign_start__lte=today))
        .filter(Q(campaign_end__isnull=True) | Q(campaign_end__gte=today))
        .filter(Q(max_airings__isnull=True) | Q(aired_count__lt=F("max_airings")))
    )


def _free_pool_qs(br: AdBreak, today):
    """契約外フリー素材プール (優先順位4。現行の均等ローテ条件のまま。#6 S8: 考査ゲート非適用)。"""
    return _free_pool_qs_for(br.grid, br.duration_ms, today)


def select_free_cms(grid: str, target_ms: int, air_at: datetime) -> list[CmCreative]:
    """grid 単位で target_ms を充填する CmCreative 列をフリー在庫から選ぶ (永続化しない)。

    fill_break のフリー割付ループ (長尺優先→同尺は aired_count 昇順の均等ローテ・同一枠内の
    素材重複回避) を AdBreak 非依存に切り出したもの。生 CM cue の grid 動的充填 (タイムキープ
    Phase3 D3 §11) が発火時に使う。埋まらない残尺は許容 (呼び出し側が clips をそのまま送る)。
    """
    unit = _unit_ms(grid)
    if target_ms < unit:
        return []
    candidates = sorted(
        _free_pool_qs_for(grid, target_ms, air_at.date()),
        key=lambda cm: (-(cm.asset.duration_ms or 0), cm.aired_count),
    )
    chosen: list[CmCreative] = []
    chosen_ids: set[int] = set()
    remaining = target_ms
    while remaining >= unit:
        pick = None
        for cm in candidates:
            dur = cm.asset.duration_ms or 0
            if dur <= 0 or dur > remaining or dur % unit != 0 or cm.asset_id in chosen_ids:
                continue
            pick = cm
            break
        if pick is None:
            break
        chosen.append(pick)
        chosen_ids.add(pick.asset_id)
        remaining -= pick.asset.duration_ms or 0
    return chosen


def fill_break(
    br: AdBreak, air_at: datetime, provider: Any = None
) -> list[tuple[AdBreakItem, CmCreative]]:
    """枠を充填し (AdBreakItem, CmCreative) ペア列を返す (#6: ad_break_item FK 用)。

    - 既存 AdBreakItem があれば再利用 (再解決の決定性。S11)。
    - 残尺ベース: duration_ms % unit == 0 かつ <= 残尺 の候補を長尺優先で詰める
      (30秒素材は 15s 枠の 2 スロット分を消費)。埋まらない残尺は呼び出し側が PLAY_FILLER。
    - provider (settings 注入) があれば candidates/accept/persisted で契約駆動割付 (sales)。
      未指定なら従来の均等ローテにデグレード (24/7 送出の安全性最優先)。
    - aired_count は実送出確定時に増分 (ここでは触らない)。
    """
    existing = list(br.items.select_related("cm_asset__asset").order_by("seq"))
    if existing:
        return [(item, item.cm_asset) for item in existing]

    unit = _unit_ms(br.grid)
    remaining = br.duration_ms
    if remaining < unit:
        return []

    today = air_at.date()
    base_qs = _free_pool_qs(br, today)
    if provider is not None:
        candidates = list(provider.candidates(br, air_at, base_qs))
    else:
        # フリー: 長尺優先 (残尺断片化防止) → 同尺は aired_count 昇順 (均等ローテ)
        candidates = sorted(base_qs, key=lambda cm: (-(cm.asset.duration_ms or 0), cm.aired_count))

    chosen: list[CmCreative] = []
    chosen_ids: set[int] = set()
    while remaining >= unit:
        pick = None
        for cm in candidates:
            dur = cm.asset.duration_ms or 0
            if dur <= 0 or dur > remaining or dur % unit != 0:
                continue
            if cm.asset_id in chosen_ids:
                continue  # 同一枠内の素材重複を避ける
            if provider is not None and not provider.accept(br, chosen, cm):
                continue  # 業種競合・考査ゲート等
            pick = cm
            break
        if pick is None:
            break
        chosen.append(pick)
        chosen_ids.add(pick.asset_id)
        remaining -= pick.asset.duration_ms or 0

    if not chosen:
        return []

    # item 永続化と placement 記録 (provider.persisted) を同一 atomic で括る (#6)
    with transaction.atomic():
        items = AdBreakItem.objects.bulk_create(
            [AdBreakItem(ad_break=br, seq=i, cm_asset=cm) for i, cm in enumerate(chosen)]
        )
        if provider is not None:
            provider.persisted(br, items)
    return list(zip(items, chosen, strict=True))


# ---- リゾルバ本体 ----


def _make_constraint_provider():
    """settings.ICSTV_FILL_CONSTRAINT_PROVIDER (dotted path) から provider を生成。

    未設定なら None → fill_break は従来の均等ローテにデグレード (S6: sales 不在でも動く)。
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    path = getattr(settings, "ICSTV_FILL_CONSTRAINT_PROVIDER", None)
    if not path:
        return None
    return import_string(path)()


# ---- 放送時間帯 (broadcast_windows) 補助関数 ----


def _on_air_dt_intervals(
    channel: Channel, t0: datetime, t1: datetime
) -> list[tuple[datetime, datetime]]:
    """broadcast_windows に基づき [t0, t1) 内の放送中 datetime 区間リストを返す。

    windows 未設定なら [(t0, t1)] = 24 時間放送。各窓は settings.TIME_ZONE (JST) で解釈する。
    end="24:00" (is_next_midnight=True) は翌日 00:00。t0/t1 でクリップしてソート済みで返す。
    """
    windows = channel.effective_broadcast_windows  # list[(start_t, end_t, is_next_midnight)]
    if not windows:
        return [(t0, t1)]
    tz = ZoneInfo(settings.TIME_ZONE)
    result: list[tuple[datetime, datetime]] = []
    start_date = t0.astimezone(tz).date()
    end_date = (t1.astimezone(tz) + timedelta(days=1)).date()
    d = start_date
    while d <= end_date:
        for w_start, w_end, is_next_midnight in windows:
            on_s = datetime.combine(d, w_start, tzinfo=tz)
            on_e = (
                datetime.combine(d + timedelta(days=1), w_end, tzinfo=tz)
                if is_next_midnight
                else datetime.combine(d, w_end, tzinfo=tz)
            )
            on_s = max(on_s, t0)
            on_e = min(on_e, t1)
            if on_s < on_e:
                result.append((on_s, on_e))
        d += timedelta(days=1)
    return sorted(result)


def _on_off_segs(
    t_start: datetime, t_end: datetime, on_intervals: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime, bool]]:
    """[t_start, t_end) を放送中(True)/休止(False)の区間リストに分割して返す。

    on_intervals はソート済み前提。放送中窓に含まれない区間はすべて休止扱い。
    """
    segs: list[tuple[datetime, datetime, bool]] = []
    cursor = t_start
    for on_s, on_e in on_intervals:
        on_s = max(on_s, t_start)
        on_e = min(on_e, t_end)
        if on_s >= on_e:
            continue
        if cursor < on_s:
            segs.append((cursor, on_s, False))
        segs.append((on_s, on_e, True))
        cursor = max(cursor, on_e)
    if cursor < t_end:
        segs.append((cursor, t_end, False))
    return segs


def _on_air_gap_ms(
    gap_start: datetime, gap_end: datetime, on_intervals: list[tuple[datetime, datetime]]
) -> int:
    """ギャップ [gap_start, gap_end) のうち放送中区間の合計ミリ秒。"""
    total = 0
    for on_s, on_e in on_intervals:
        s = max(on_s, gap_start)
        e = min(on_e, gap_end)
        if s < e:
            total += int((e - s) / timedelta(milliseconds=1))
    return total


# 休止明け直前のスレート発行を打ち切るガード幅。休止中は resolve 周期 (5分) ごとに
# 「now」でスレートを再発行するが、窓終了の直前で発行すると agent への gRPC 到達 +
# ディスパッチ遅延 (実測 1〜6 秒) の間に休止明けの CLEAR_SLATE が先に実行され、後から
# 届いたスレートが本線の上へ再点灯して固着する (2026-07-21 06:00: 05:59:59 発行の
# スレートが 06:00:01 に実行され、運用者が手動解除する 08:21 まで 2h21m スレート固着)。
# スレートは loop=True で既に出続けているため、休止明け直前の 1 発は落として無害。
_STANDBY_TAIL_GUARD = timedelta(seconds=30)


def _on_air_ms(channel: Channel, start: datetime, stop: datetime) -> int:
    """[start, stop) の放送中ミリ秒 (broadcast_windows 未設定なら実時間そのもの)。

    フィラーの位相は放送中時間だけで進むという不変条件 (docs/scheduler.md) の実体。窓を任意区間で
    引き直すので、resolve の on_intervals (t0 でクリップ済み) が届かない過去区間にも使える。
    """
    if stop <= start:
        return 0
    if not channel.broadcast_windows:
        return int((stop - start) / timedelta(milliseconds=1))
    return _on_air_gap_ms(start, stop, _on_air_dt_intervals(channel, start, stop))


def _on_air_window_start(channel: Channel, at: datetime) -> datetime | None:
    """at を含む放送中窓の開始時刻 (t0 クリップ前の窓幾何)。休止中なら None。

    resolve の on_intervals は t0 でクリップされるため区間先頭が「now」になり得る。休止明けの
    再開アンカーには、周期再 resolve でも動かない「窓オープン時刻」そのものが要る (動く値を
    anchor にすると再生中クリップを毎周期奪う旧不具合に戻る)。
    """
    for on_s, on_e in _on_air_dt_intervals(channel, at - timedelta(days=2), at + timedelta(days=1)):
        if on_s <= at < on_e:
            return on_s
    return None


def _on_air_subsegments(
    channel: Channel, start: datetime, stop: datetime
) -> list[tuple[datetime, datetime]]:
    """[start, stop) の放送中区間 (broadcast_windows 未設定なら区間そのもの)。"""
    if start >= stop:
        return []
    if not channel.broadcast_windows:
        return [(start, stop)]
    return [
        (s, e)
        for s, e, on in _on_off_segs(start, stop, _on_air_dt_intervals(channel, start, stop))
        if on
    ]


def _filler_seg_anchor(
    channel: Channel,
    seg_s: datetime,
    anchor: datetime,
    *,
    leading: bool,
    prev_end: datetime | None,
) -> datetime:
    """放送中区間 [seg_s, ...) に敷くフィラーサイクルの anchor (位相原点)。

    位相原点は resolve 周期で動いてはならない (動かすと再生中クリップを毎周期奪う旧不具合に戻る)。
    候補は「同じ放送中窓の中で終わった直近番組の end_at」か「窓オープン時刻」の 2 つだけで、
    cursor=t0=now は採らない。

    - 先頭ギャップ (leading): prev_end が同じ窓の中ならそれ、そうでなければ (窓より前 / 番組が
      無い) 窓オープン時刻。休止でスキップされた番組は _last_program_end_before が既に除外済み。
    - 番組明けギャップ: anchor (= 前番組の end_at) が同じ窓の中ならそれ、休止をまたぐなら窓オープン。
    """
    win_open = _on_air_window_start(channel, seg_s) or seg_s
    if leading:
        return prev_end if (prev_end is not None and prev_end >= win_open) else win_open
    return anchor if anchor >= win_open else win_open


def _filler_seg_phase(
    channel: Channel,
    durs: list[int],
    seg_s: datetime,
    anchor: datetime,
    *,
    first_on: bool,
    leading: bool,
    prev_end: datetime | None,
    start_idx: int,
    start_offset_ms: int,
    filler_elapsed_ms: int,
) -> tuple[datetime, int, int]:
    """放送中区間 [seg_s, ...) に敷くフィラーの (anchor, start_idx, start_offset_ms) を決める。

    - 同一放送中窓の中で継続する先頭区間: 従来どおり呼び出し側の anchor と位相 (実際の起点は
      emit_filler 内の鎖状継続 _onair_filler_anchor が決める)。
    - 休止をまたぐ先頭区間 (= 休止明け) と 2 本目以降の on 区間: 区間先頭 (窓オープン) を anchor に
      し、その時点の位相 (_filler_phase_at) から再開する。位相は「発行済みイベントの位相 + そこから
      の放送中経過時間」で決まるので、途中の境界イベントを見なくても正しく、resolve 周期でも冪等。
    - 発行済みイベントがまだ無い (チャンネル開始直後 / テスト) ときだけ、resolve 内で前方に積算した
      filler_elapsed_ms にフォールバックする。
    """
    seg_anchor = (
        _filler_seg_anchor(channel, seg_s, anchor, leading=leading, prev_end=prev_end)
        if first_on
        else seg_s
    )
    if not durs:
        return seg_anchor, start_idx, start_offset_ms
    if first_on and seg_anchor == anchor:
        return anchor, start_idx, start_offset_ms
    phase = _filler_phase_at(channel, durs, seg_anchor)
    if phase is not None:
        return seg_anchor, phase[0], phase[1]
    if first_on:
        return seg_anchor, start_idx, start_offset_ms
    idx, offset = _filler_resume_index(durs, filler_elapsed_ms)
    return seg_anchor, idx, offset


def emit_standby(
    channel: Channel,
    t_start: datetime,
    t_end: datetime,
    *,
    not_before: datetime,
) -> list[PendingEvent]:
    """休止時間帯の先頭に PLAY_SLATE を 1 イベント発行 (resolver 管轄スレート)。

    off_air=True param で operator-slate と区別し、_commit() の cancel 除外対象から外す。
    エージェントは既存の PLAY_SLATE 処理でスレート素材をループ再生する。

    休止明け (t_end) まで _STANDBY_TAIL_GUARD 未満しか残っていない発行は行わない
    (CLEAR_SLATE との競合でスレートが再点灯し固着するため)。休止区間そのものがガード幅
    より短い場合もスレートを出さない (一瞬だけスレートを差し込む方が視聴体験として悪い)。
    """
    at = max(t_start, not_before)
    if at >= t_end - _STANDBY_TAIL_GUARD:
        return []
    return [
        _pe(
            channel.id,
            at,
            PlayoutAction.PLAY_SLATE,
            "off_air",
            params={
                "off_air": True,
                "until": t_end.isoformat(),
                "loop": True,
            },
        )
    ]


def _needs_slate_clear(
    channel: Channel,
    before: datetime,
    *,
    now: datetime,
    pending: list[PendingEvent],
) -> bool:
    """resolver 管轄の休止スレート (PLAY_SLATE off_air=True) が、それより後の CLEAR_SLATE で
    解除されないまま残っているか判定する。

    layer 90 (スレート) は layer 10 (本線) と独立レイヤなので、休止明けに本線側 (emit_filler)
    が再開しても CLEAR_SLATE を明示発行しない限りスレートが本線の上に残り続ける (本線は復帰して
    いるのに画面がスレート固着のまま=本不具合)。「直近の off_air PLAY_SLATE より後に CLEAR_SLATE
    が無い」ときだけ True にすることで、まだ解除していない休止明けを検出しつつ、既に解除済みの
    区間を resolve 再実行のたびに再判定しない (= 運用者の手動/緊急スレートを奪い返さない。手動
    スレートは off_air param を持たないため last_slate の対象外)。
    before で「まだ来ていない未来の off_air スレート」を除外し、今より先の休止を早取りしない。

    pending: この resolve 実行内で既に積んだ未コミットイベントも時系列判定に含める (DB だけ見ると
    同一実行内の off_air スレート/CLEAR が見えず、番組直前チェックが重複 CLEAR を発行してしまう)。
    now: 「解除済み」の根拠が未来の SCHEDULED CLEAR しか無いときは True を返し、同じ scheduled_at
    (=同じ idempotency_key) で再発行して upsert 更新にする。再発行をやめると次の _commit が
    「今回の解決に無いイベント」として CANCELLED 化→次周期で復活…をビートごとに繰り返し、
    休止明け時刻にちょうど CANCELLED 側だと agent が実行せずスレートが残る (フラップ不具合)。
    """
    last_slate = (
        PlayoutEvent.objects.filter(
            channel=channel,
            action=PlayoutAction.PLAY_SLATE,
            scheduled_at__lte=before,
            params__contains={"off_air": True},
        )
        .exclude(status=PlayoutStatus.CANCELLED)
        .order_by("-scheduled_at")
        .first()
    )
    last_slate_at = last_slate.scheduled_at if last_slate else None
    for pe in pending:
        if (
            pe.action == PlayoutAction.PLAY_SLATE
            and (pe.params or {}).get("off_air")
            and pe.scheduled_at <= before
            and (last_slate_at is None or pe.scheduled_at > last_slate_at)
        ):
            last_slate_at = pe.scheduled_at
    if last_slate_at is None:
        return False
    for pe in pending:
        if pe.action == PlayoutAction.CLEAR_SLATE and last_slate_at < pe.scheduled_at <= before:
            return False
    already_cleared = PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.CLEAR_SLATE,
        scheduled_at__gt=last_slate_at,
        scheduled_at__lte=before,
    ).exclude(status=PlayoutStatus.CANCELLED)
    # DONE/EXECUTING や実行期限が来た (過去の) SCHEDULED は解除済み。未来の SCHEDULED しか
    # 無ければ再発行して保持する (同 key の upsert なので増殖しない)。
    settled = already_cleared.exclude(status=PlayoutStatus.SCHEDULED, scheduled_at__gte=now)
    if not settled.exists():
        return True
    # ここまでで「scheduled_at 上は解除済み」。ただし agent の実行順は scheduled_at 順とは
    # 限らない: 休止明け直前に発行されたスレートは gRPC 到達 + ディスパッチ遅延で CLEAR_SLATE
    # より後に実行され、本線の上へ再点灯したまま固着する (2026-07-21 06:00 の事故)。
    # 実行時刻 (actual_at) で逆転を検出し、解除されていなければ再発行して自己修復する
    # (次ビート = 最大 5 分で復旧。従来は運用者が手動解除するまで固着し続けた)。
    # pending 由来の最新スレートは未実行なので対象外 (DB の実績のみで判定する)。
    if (
        last_slate is None
        or last_slate.scheduled_at != last_slate_at
        or last_slate.actual_at is None
    ):
        return False
    return not PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.CLEAR_SLATE,
        actual_at__gt=last_slate.actual_at,
    ).exists()


def emit_resume(channel: Channel, at: datetime, *, not_before: datetime) -> list[PendingEvent]:
    """放送再開 (on-air 区間先頭) に CLEAR_SLATE を 1 イベント発行 (emit_standby の対)。

    呼び出し側 (_needs_slate_clear) が「まだ解除していない」ときだけ呼ぶ前提。CLEAR_SLATE 自体は
    何度実行されても無害 (空 layer への CLEAR) だが、無条件に毎 resolve 周期で発行すると
    運用者が別途出した手動/緊急スレートまで次の resolve で消してしまうため、ここでは呼ばない。
    """
    return [_pe(channel.id, max(at, not_before), PlayoutAction.CLEAR_SLATE, "resume")]


def resolve(channel: Channel, t0: datetime, t1: datetime) -> dict[str, int]:
    """[t0, t1) の編成を解決し playout_event を冪等 upsert。

    Returns: {"created": N, "updated": N, "cancelled": N, "revived": N, "skipped": N}
    """
    pending: list[PendingEvent] = []
    provider = _make_constraint_provider()  # resolve 1 実行 = provider 1 インスタンス (#6)

    # broadcast_windows が設定されていれば放送中区間を算出し、休止中に開始する番組をスキップ。
    has_windows = bool(channel.broadcast_windows)
    on_intervals = _on_air_dt_intervals(channel, t0, t1) if has_windows else []

    programs = list(
        Program.objects.filter(
            channel=channel,
            end_at__gt=t0,
            start_at__lt=t1,
        )
        .select_related("asset", "live_source", "channel", "series", "live_rundown")
        .order_by("start_at")
    )
    if has_windows:
        # 休止中に開始する番組はスキップ。判定は「開始時刻が放送時間帯内か」(未クリップの窓
        # 幾何) で行う。t0 でクリップ済みの on_intervals で判定すると、放送中に開始済みの番組
        # (start_at < t0=now) まで resolve のたびに除外され、その残り尺がギャップ扱いになって
        # フィラーが被せ発行され本編を奪ってしまう。
        programs = [p for p in programs if channel.is_on_air(p.start_at)]

    cursor = t0
    # 先頭ギャップ (現在再生中) の位相は t0=now ではなく直近終了 program の end_at に固定する
    # (resolve 周期で scheduled_at がぶれて再生中フィラーを奪うのを防ぐ)。前 program が無ければ
    # cursor=t0 を起点にする (チャンネル開始直後の真の起点)。
    prev_end = _last_program_end_before(channel, t0)
    # 実番組明けのギャップは playlist 先頭に戻さず「リスト上の次」から継ぐ。実番組の尺を差し引いた
    # フィラー累計実時間 (filler_elapsed_ms) から再開ローテ位置を算出する (ステートレス・冪等)。
    # 先頭ギャップは従来どおり anchor 位相 (start_idx=0)。番組が future→past に移っても、その瞬間に
    # 先頭となるギャップ先頭クリップは既に on-air なので _onair_filler_anchor が鎖状継続で同 index を
    # 再現し、リセットも index ずれも起きない。
    # broadcast_windows 有効時: フィラー経過時間は放送中区間のみ積算 (off-air 時間は除外)。
    durs = _filler_durs(channel)
    total_dur = sum(durs)
    filler_elapsed_ms = 0
    leading = True
    for prog_idx, prog in enumerate(programs):
        anchor = prev_end if (leading and prev_end is not None) else cursor
        start_idx, start_offset_ms = (
            (0, 0) if (leading or not durs) else _filler_resume_index(durs, filler_elapsed_ms)
        )
        if prog.start_at > cursor:
            if has_windows:
                # ギャップを放送中/休止に分割して発行。
                # 先頭 on 区間は anchor を継承 (位相安定); 後続 on 区間は区間先頭を新 anchor にする。
                # 「先頭」は _on_off_segs の出走順で判定する (seg_s <= anchor という値比較だと、
                # on_intervals 自体が resolve 呼び出しの t0=now でクリップされるため seg_s が
                # resolve 再実行のたびに前進し、anchor より後ろになった瞬間に「後続」誤判定され
                # 送出中クリップの位相安定 (_onair_filler_anchor) が壊れて同一クリップが再発行され
                # 続けるバグがあった (#放送休止機能導入時の回帰))。
                first_on = True
                for seg_s, seg_e, is_on in _on_off_segs(cursor, prog.start_at, on_intervals):
                    if is_on:
                        seg_anchor, seg_idx, seg_offset = _filler_seg_phase(
                            channel,
                            durs,
                            seg_s,
                            anchor,
                            first_on=first_on,
                            leading=leading,
                            prev_end=prev_end,
                            start_idx=start_idx,
                            start_offset_ms=start_offset_ms,
                            filler_elapsed_ms=filler_elapsed_ms,
                        )
                        first_on = False
                        # 本線 (layer 10) を先に pending へ積み、スレート解除 (layer 90) を後に置く。
                        # 休止明けは両者が窓オープン時刻で同着するため、agent の due_for_take
                        # (scheduled_at 昇順のみ) でも「正しい位置のフィラー → スレート解除」の順に
                        # なりやすくする (LOADBG は preroll 済みなので定刻の送信は take 1 本)。
                        pending.extend(
                            emit_filler(
                                channel,
                                seg_anchor,
                                seg_e,
                                not_before=t0,
                                next_program=prog,
                                start_idx=seg_idx,
                                start_offset_ms=seg_offset,
                            )
                        )
                        if _needs_slate_clear(channel, seg_s, now=t0, pending=pending):
                            pending.extend(emit_resume(channel, seg_s, not_before=t0))
                        # 非 leading は区間ごとに積算 (leading は下の onair anchor ブロックで一括処理)
                        if durs and not leading:
                            filler_elapsed_ms += int(
                                (seg_e - max(seg_s, anchor)) / timedelta(milliseconds=1)
                            )
                    else:
                        pending.extend(emit_standby(channel, seg_s, seg_e, not_before=t0))
            else:
                # 24/7: 既存ロジック。このギャップ後の番組 prog が「次番組」(予告対象)。
                pending.extend(
                    emit_filler(
                        channel,
                        anchor,
                        prog.start_at,
                        not_before=t0,
                        next_program=prog,
                        start_idx=start_idx,
                        start_offset_ms=start_offset_ms,
                    )
                )
        # ギャップ実時間をフィラー累計に積む (発行を t0 で打ち切っても概念ギャップ全体を数える)。
        if durs and prog.start_at > anchor:
            # 先頭ギャップは「clip0 起点の仮想サイクル」ではなく発行済みイベントの実位相を使う。
            # resolve 毎に prev_end が変わると仮想位相が毎回ずれ、後続ギャップの start_idx が
            # 不定になる (playlist の先頭クリップが永久に流れない等)。_filler_phase_at は
            # 放送中時間だけで位相を進めるので、休止をまたいでも中断位置がそのまま復元される。
            phase = (
                _filler_phase_at(channel, durs, prog.start_at) if (leading and total_dur) else None
            )
            if phase is not None:
                filler_elapsed_ms = (sum(durs[: phase[0]]) + phase[1]) % total_dur
            elif leading:
                # 発行済みイベントが無い (チャンネル開始直後 / テスト) → anchor 起点の実経過。
                filler_elapsed_ms += _on_air_ms(channel, anchor, prog.start_at)
            elif not has_windows:
                # broadcast_windows 有効時は上のセグメントループで積算済み。
                filler_elapsed_ms += int((prog.start_at - anchor) / timedelta(milliseconds=1))
        # 番組が窓オープンちょうど (休止明け=番組開始) に始まると、直前ギャップに on-air
        # フィラー区間が無く上のセグメントループでは CLEAR_SLATE が出ない。番組直前でも解除
        # 判定する (通常の窓中番組は直前フィラー区間の CLEAR が pending/DB に見えて no-op)。
        # before=max(start_at, t0) で catch-up (番組開始後に気づいた) 時も即時解除する。
        if has_windows and _needs_slate_clear(
            channel, max(prog.start_at, t0), now=t0, pending=pending
        ):
            pending.extend(emit_resume(channel, prog.start_at, not_before=t0))
        if prog.type == ProgramType.RECORDED:
            pending.extend(emit_recorded(prog, provider=provider))
        else:
            pending.extend(emit_live(prog))
        pending.extend(
            emit_exposure_mirror(
                prog,
                prev_prog=programs[prog_idx - 1] if prog_idx > 0 else None,
                next_prog=programs[prog_idx + 1] if prog_idx + 1 < len(programs) else None,
            )
        )
        cursor = prog.end_at
        leading = False
    if cursor < t1:
        anchor = prev_end if (leading and prev_end is not None) else cursor
        start_idx, start_offset_ms = (
            (0, 0) if (leading or not durs) else _filler_resume_index(durs, filler_elapsed_ms)
        )
        if has_windows:
            first_on = True
            for seg_s, seg_e, is_on in _on_off_segs(cursor, t1, on_intervals):
                if is_on:
                    seg_anchor, seg_idx, seg_offset = _filler_seg_phase(
                        channel,
                        durs,
                        seg_s,
                        anchor,
                        first_on=first_on,
                        leading=leading,
                        prev_end=prev_end,
                        start_idx=start_idx,
                        start_offset_ms=start_offset_ms,
                        filler_elapsed_ms=filler_elapsed_ms,
                    )
                    first_on = False
                    # 末尾ギャップの次番組は窓外を1件引く (あれば予告)。
                    # 本線を先に積んでからスレート解除 (窓オープンで同着するため。上の per-program
                    # ループと同じ理由)。
                    pending.extend(
                        emit_filler(
                            channel,
                            seg_anchor,
                            seg_e,
                            not_before=t0,
                            next_program=_next_program(channel, t1),
                            start_idx=seg_idx,
                            start_offset_ms=seg_offset,
                        )
                    )
                    if _needs_slate_clear(channel, seg_s, now=t0, pending=pending):
                        pending.extend(emit_resume(channel, seg_s, not_before=t0))
                    if durs:
                        filler_elapsed_ms += int(
                            (seg_e - max(seg_s, anchor)) / timedelta(milliseconds=1)
                        )
                else:
                    pending.extend(emit_standby(channel, seg_s, seg_e, not_before=t0))
        else:
            # 末尾ギャップの次番組は窓外を1件引く (あれば予告)。
            pending.extend(
                emit_filler(
                    channel,
                    anchor,
                    t1,
                    not_before=t0,
                    next_program=_next_program(channel, t1),
                    start_idx=start_idx,
                    start_offset_ms=start_offset_ms,
                )
            )

    return _commit(channel, t0, t1, pending)


def _commit(
    channel: Channel,
    t0: datetime,
    t1: datetime,
    pending: list[PendingEvent],
) -> dict[str, int]:
    stats = {"created": 0, "updated": 0, "cancelled": 0, "revived": 0, "skipped": 0}

    with transaction.atomic():
        # Phase2 二重発火防止 (レビューで実証された TOCTOU レースの修正): pending は resolve()
        # 冒頭の非ロック読み取りから構築される。この読み取りと本コミットの間に手動発火/スキップ
        # (fire_cm_now_cue/roll_vt_cue/skip_live_cue、いずれも LiveCue.select_for_update 使用) が
        # 割り込むと、古いスナップショットのまま「手動キャンセル直後の auto_fire 予約」を
        # 復活/新規作成してしまい実際に二重発火しうる (D1/D3 だけでは resolve 内で完結する
        # レースを防げない)。ここで該当 LiveCue を select_for_update により確保し直すことで
        # 手動発火系と真に排他し (Postgres の行ロックで直列化)、PENDING を外れた cue の
        # イベントを pending から除外してから確定させる。
        live_cue_ids: set[int] = {
            int(cid) for pe in pending if (cid := pe.params.get("live_cue_id"))
        }
        if live_cue_ids:
            still_pending = set(
                LiveCue.objects.select_for_update()
                .filter(pk__in=live_cue_ids, state=LiveCueState.PENDING)
                .values_list("pk", flat=True)
            )
            pending = [
                pe
                for pe in pending
                if not pe.params.get("live_cue_id") or pe.params["live_cue_id"] in still_pending
            ]

        new_keys = {pe.idempotency_key for pe in pending}

        # 旧解決にあり今回消えた SCHEDULED event を CANCELLED に (tombstone 伝搬)。
        # PLAY_SLATE の扱い: 運用者が手動投入した slate (params に off_air キーなし) は resolver
        # 管轄外なので除外する。resolver が broadcast_windows 休止用に発行した slate
        # (params["off_air"]=True) は通常通り管轄内とし window 変更時に正しく差し替わる。
        cancelled = (
            PlayoutEvent.objects.filter(
                channel=channel,
                scheduled_at__gte=t0,
                scheduled_at__lt=t1,
                status=PlayoutStatus.SCHEDULED,
            )
            .exclude(idempotency_key__in=new_keys)
            .exclude(Q(action=PlayoutAction.PLAY_SLATE) & ~Q(params__contains={"off_air": True}))
            .update(
                status=PlayoutStatus.CANCELLED,
            )
        )
        stats["cancelled"] = cancelled

        existing_map = {
            ev.idempotency_key: ev
            for ev in PlayoutEvent.objects.filter(idempotency_key__in=new_keys)
        }

        for pe in pending:
            defaults = {
                "channel_id": pe.channel_id,
                "scheduled_at": pe.scheduled_at,
                "action": pe.action,
                "asset_id": pe.asset_id,
                "live_source_id": pe.live_source_id,
                "cm_bundle_id": pe.cm_bundle_id,
                "program_id": pe.program_id,
                "ad_break_item_id": pe.ad_break_item_id,
                "params": pe.params,
            }
            existing = existing_map.get(pe.idempotency_key)
            if existing is None:
                PlayoutEvent.objects.create(
                    idempotency_key=pe.idempotency_key,
                    status=PlayoutStatus.SCHEDULED,
                    **defaults,
                )
                stats["created"] += 1
            elif existing.status == PlayoutStatus.CANCELLED:
                # 復活: 編成で削除取り消し等
                for k, v in defaults.items():
                    setattr(existing, k, v)
                existing.status = PlayoutStatus.SCHEDULED
                existing.save()
                stats["revived"] += 1
            elif existing.status == PlayoutStatus.SCHEDULED:
                for k, v in defaults.items():
                    setattr(existing, k, v)
                existing.save()
                stats["updated"] += 1
            else:
                # EXECUTING/DONE/SKIPPED/FAILED は不変 (実行履歴を保護)
                stats["skipped"] += 1
    return stats
