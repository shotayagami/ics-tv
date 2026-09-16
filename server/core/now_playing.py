# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開トップ向け「いま放送中」カードの解決 (#7 ライブ表示)。

公開トップは従来 Program (編成表) のみで現在番組を判定していたため、フィラー時間帯や
編成が公開設定されていない時刻は LIVE 扱いにならず「準備中」+黒箱になっていた。ここでは:

  - LIVE バッジ/ライブ静止画の点灯可否は「agent が実際に送出中か」(AgentStatus heartbeat) で判定。
    編成の有無に依存しないため、フィラー中でも正しく LIVE 表示になる。
  - タイトルは実出力 (最新の非 CANCELLED PlayoutEvent → program/asset) を優先し、無ければ
    編成 Program、どちらも無ければ online なら「放送中」/ offline なら「準備中」。

ops_views._on_air と同じ on-air 規約 (scheduled_at<=now の最新非 CANCELLED) を踏襲する。
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.genre import genre_color
from playout.models import AgentStatus, PlayoutAction, PlayoutEvent, PlayoutStatus

# AgentStatus.last_heartbeat_at がこれを超えて途絶したら offline (ops_views._OFFLINE_THRESHOLD と揃える)。
_OFFLINE_THRESHOLD = timedelta(seconds=90)

# 主映像出力を差し替えない overlay/transition 系 action。これらは「現在番組」の解決対象外。
# (例: フィラー再生中に layer40 速報テロップを出す overlay_op が最新 event になっても、
#  本線で流れているのはその下の PLAY_FILLER。これを除外しないと現在番組がタイトル無しに化ける。)
_NON_CONTENT_ACTIONS = (
    PlayoutAction.OVERLAY_OP,
    PlayoutAction.YT_TRANSITION,
    PlayoutAction.CLEAR_SLATE,
)


def is_online(channel, now) -> bool:
    """agent が直近 heartbeat 内に生存しているか (= 実際に送出している見込み)。"""
    status = AgentStatus.objects.filter(channel=channel).first()
    return bool(
        status
        and status.last_heartbeat_at
        and (now - status.last_heartbeat_at) <= _OFFLINE_THRESHOLD
    )


def is_broadcast_paused(channel, now) -> bool:
    """broadcast_windows による意図的な放送休止中か。

    agent は休止スレート (PLAY_SLATE) を送出中で is_online は True のままなので、
    「LIVE 表示してよいか」は online とは別にこれで判定する。
    """
    return bool(channel.effective_broadcast_windows) and not channel.is_on_air(now)


def next_on_air_label(channel, now) -> str | None:
    """休止中に「次回いつ放送再開か」を公開表示するための短いラベル (JST)。放送中/未設定は None。

    当日中なら "HH:MM"、翌日なら "明日 HH:MM"、それ以降は "M/D HH:MM"。
    """
    nxt = channel.next_on_air(now)
    if nxt is None:
        return None
    ldt = timezone.localtime(nxt)
    days = (ldt.date() - timezone.localtime(now).date()).days
    hm = f"{ldt:%H:%M}"
    if days <= 0:
        return hm
    if days == 1:
        return f"明日 {hm}"
    return f"{ldt:%-m/%-d} {hm}"


def resolve_on_air_event(channel, now) -> PlayoutEvent | None:
    """channel の now 時点で「今出ている」実行チェーン上のイベント (overlay/transition/clear_slate
    除外)。on_air_info と core.timekeeper が共有し、除外ルールの二重実装を避ける。"""
    return (
        PlayoutEvent.objects.filter(channel=channel, scheduled_at__lte=now)
        .exclude(status=PlayoutStatus.CANCELLED)
        .exclude(action__in=_NON_CONTENT_ACTIONS)
        .select_related("program", "asset", "cm_bundle")
        .order_by("-scheduled_at")
        .first()
    )


def on_air_info(channel, now) -> dict | None:
    """実出力の現在イベントから視聴者向けの放送情報を解決。

    返り値 (どれも視聴者にタイトルを出せるイベントのときのみ。フィラー非対象/スレート等は None):
      - title:    視聴者向けタイトル
      - is_rerun: 再放送フィラー (rerun_eligible 素材) なら True
      - start/end: 再放送クリップの送出区間 (進行バー用)。番組内は None (編成 Program 側で算出)。

    再放送露出: PLAY_FILLER でも素材が rerun_eligible なら「再放送」として素材タイトルを出す。
    既定 (rerun_eligible=False) のフィラーは従来どおり title を出さない (None)。
    """
    event = resolve_on_air_event(channel, now)
    if event is None:
        return None
    # 番組内 (recorded 本編/CM/live) は番組名を出すのが視聴者に分かりやすい。
    if event.program_id and event.program:
        return {"title": event.program.title, "is_rerun": False, "start": None, "end": None}
    if event.action == PlayoutAction.PLAY_ASSET and event.asset_id and event.asset:
        return {"title": event.asset.title, "is_rerun": False, "start": None, "end": None}
    if (
        event.action == PlayoutAction.PLAY_FILLER
        and event.asset_id
        and event.asset
        and event.asset.rerun_eligible
    ):
        until = parse_datetime(event.params.get("until") or "") if event.params else None
        return {
            "title": event.asset.title,
            "is_rerun": True,
            "start": event.scheduled_at,
            "end": until,
        }
    return None  # rerun 非対象 filler / play_slate 等は番組名なし


def on_air_title(channel, now) -> str:
    """実出力の現在イベントから視聴者向けタイトルを解決。不明なら空 (後方互換の薄いラッパ)。"""
    info = on_air_info(channel, now)
    return info["title"] if info else ""


def card(channel, now, current=None, upcoming=None, member=None) -> dict:
    """公開トップのカード 1 枚分のコンテキスト (テンプレ + /api/now・/api/v1/home で共用)。

    current / upcoming は呼び出し側で算出済みの編成 Program (フォールバック表示用)。member は
    exposure_policy/ファンクラブ ティア軸のゲート判定用 (#27 Phase B)。共有 Live Input
    (Channel.cf_playback_hls_url は全番組で同一URL) は api.routers.player.channel_detail と
    同じ scheduling.exposure_gate.hls_url_for を経由しないと「ホームのライブプレビューだけ
    ゲートを素通りする」事故が起きるため、hls_url は必ずこの関数経由でのみ解決する。
    """
    from scheduling.exposure_gate import can_watch_live, gate_reason, hls_url_for

    online = is_online(channel, now)
    paused = is_broadcast_paused(channel, now)
    info = on_air_info(channel, now)
    title = info["title"] if info else ""
    is_rerun = bool(info and info["is_rerun"])
    if not title and current is not None:
        title = current.title
    if title:
        nowtitle = title
    elif paused:
        nowtitle = "放送休止中"
    elif online:
        nowtitle = "放送中"
    else:
        nowtitle = "準備中"

    # 公開フロント (#7 デザイン刷新) 用の表示メタ。time_range / 進行バー / カウントダウンは
    # 編成 Program (current) があれば編成尺、無くても再放送フィラーならクリップ送出区間で出す
    # (「あたかも編成があったように」)。countdown/progress はクライアントが epoch から毎秒算出。
    genre = current.resolved_genre if current is not None else ""
    if current is not None:
        cs = timezone.localtime(current.start_at)
        ce = timezone.localtime(current.end_at)
        time_range = f"{cs:%H:%M} – {ce:%H:%M}"
        cur_start_ts: int | None = int(current.start_at.timestamp())
        cur_end_ts: int | None = int(current.end_at.timestamp())
    elif info and info["is_rerun"] and info["start"] and info["end"]:
        cs = timezone.localtime(info["start"])
        ce = timezone.localtime(info["end"])
        time_range = f"{cs:%H:%M} – {ce:%H:%M}"
        cur_start_ts = int(info["start"].timestamp())
        cur_end_ts = int(info["end"].timestamp())
    else:
        time_range = ""
        cur_start_ts = cur_end_ts = None
    next_title = upcoming.title if upcoming is not None else ""
    next_time = f"{timezone.localtime(upcoming.start_at):%H:%M}" if upcoming is not None else ""

    # exposure_policy (#27) + ファンクラブ ティア軸 (#27 Phase B、docs/fanclub.md §6.4)。
    entitled = can_watch_live(current, member)

    return {
        "channel": channel,
        "slug": channel.slug,
        "name": channel.name,
        "short": channel.short_name,
        "tint": channel.tint_color,
        "online": online,
        "live": online and not paused,
        "title": title,
        "nowtitle": nowtitle,
        "is_rerun": is_rerun,
        "genre": genre,
        "genre_color": genre_color(genre),
        "time_range": time_range,
        "cur_start_ts": cur_start_ts,
        "cur_end_ts": cur_end_ts,
        "next_title": next_title,
        "next_time": next_time,
        "poster": f"/live/{channel.slug}/poster.jpg",
        "hls_url": (hls_url_for(channel, current, member) or "") if entitled else "",
        "gate_reason": "" if entitled else gate_reason(current, member),
        "fallback_thumb": current.thumb_url if current is not None else "",
        "current": current,
        "upcoming": upcoming,
    }
