# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開番組表 (EPG タイムライングリッド) の幾何 + 派生ビューの算出 (#7 デザイン刷新)。

design_handoff (ICS-TV.dc.html の buildEpg / dayList) を Django へ翻訳したもの。DB アクセスは
呼び出し側 (views) が済ませ、ここは Program のリストを受け取って純粋に座標/色/表示文字列へ変換する。

座標系はデザインの pxPerMin=1.5 を踏襲しつつ、24h リニア編成に合わせて当日 00:00–24:00 (0–1440分)
の全日窓へ適応する (デザインのサンプルは 06:00–24:00 固定)。日跨ぎ番組は当日窓へクランプする。
"""

from __future__ import annotations

from datetime import datetime, time

from django.utils import timezone

from core.genre import genre_color

# 縦密度 (px/分) の既定。ユーザは番組表 UI のズーム (− / + / リセット) で client 側に上書きでき、
# その値は localStorage に保持される。ここはあくまで初期密度 / 非 JS フォールバックの基準。
# 1.5 だと短尺番組 (≲23分) が最小高 (MIN_BLOCK_H) に潰れ時刻+タイトルが読めないため 2.2 を既定とする。
PX_PER_MIN = 2.2
DAY_MIN = 24 * 60  # 1440
# ブロック最小高 (px) と隣接ブロックとの隙間 (px)。client 側のズーム再計算もこの式を共有する
# (height = max(dur_min * ppm - BLOCK_GAP, MIN_BLOCK_H))。短尺は最小高で潰れるので本文は適応表示する。
MIN_BLOCK_H = 30
BLOCK_GAP = 4

# EPG ブロックの静的見た目 (放送中 / 通常)。動的な top/height/border-left 色は build 時に合成。
_LIVE_BG, _LIVE_BORDER = "#192243", "var(--accent)"
_NORMAL_BG, _NORMAL_BORDER = "#10172e", "#222b45"
_LIVE_SHADOW = "0 6px 24px var(--accent-glow)"
# 再放送 (フィラー由来) ブロックの左帯色 (ジャンル色が無いときのミュート既定)。
_RERUN_BORDER = "#5b6480"


def _day_start(base_date):
    """ローカル日 base_date の 00:00 (aware)。"""
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(base_date, time.min), tz)


def _minutes(dt, day_start) -> float:
    return (dt - day_start).total_seconds() / 60.0


# 月=0 起点 (date.weekday())。週間グリッドの列ヘッダ用。
_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _build_block(p, day_start, tint, is_live, px_per_min, *, is_rerun=False):
    """1 番組ブロックの座標/色/表示文字列。当日窓に掛からない (クランプで潰れた) なら None。

    日(全ch)グリッドと週(1ch×7日)グリッドで共通利用する。is_rerun はフィラー由来の再放送
    ブロック (編成 Program ではない合成行。番組詳細リンクは張らず「再放送」バッジを出す)。
    """
    st = max(0.0, _minutes(p.start_at, day_start))
    en = min(float(DAY_MIN), _minutes(p.end_at, day_start))
    if en <= st:
        return None
    top = st * px_per_min
    h = max((en - st) * px_per_min - BLOCK_GAP, MIN_BLOCK_H)
    # 再放送ブロックは編成より控えめに (左帯はミュート色)。放送中は通常どおり強調する。
    color = genre_color(p.resolved_genre) or (_RERUN_BORDER if is_rerun else tint)
    bg = _LIVE_BG if is_live else _NORMAL_BG
    border = _LIVE_BORDER if is_live else _NORMAL_BORDER
    shadow = _LIVE_SHADOW if is_live else "none"
    style = (
        f"position:absolute;left:6px;right:6px;top:{top:.1f}px;height:{h:.1f}px;"
        f"padding:8px 11px;border-radius:9px;overflow:hidden;background:{bg};"
        f"border:1px solid {border};border-left:4px solid {color};box-shadow:{shadow};"
    )
    ls = timezone.localtime(p.start_at)
    le = timezone.localtime(p.end_at)
    return {
        "id": p.id,
        "title": p.title,
        "genre": p.resolved_genre,
        "time": f"{ls:%H:%M}–{le:%H:%M}",
        "color": color,
        "is_live": is_live,
        "style": style,
        "time_color": "var(--accent)" if is_live else "#8b93a7",
        "title_color": "#eef1f7" if is_live else "#cdd3e0",
        # 構造化幾何 (#Phase2 guide 島が React 側で style を組む。style 文字列は week.html が継続利用)。
        "top": round(top, 1),
        "height": round(h, 1),
        # 当日窓へクランプ済みの生の分オフセット/尺。client のズームが top/height を
        # 任意 px/分で再計算するための素データ (top/height は既定 px_per_min での初期値)。
        "start_min": round(st, 2),
        "dur_min": round(en - st, 2),
        "bg": bg,
        "border": border,
        "shadow": shadow,
        "is_rerun": is_rerun,
    }


def build_epg(columns, base_date, now, *, px_per_min: float = PX_PER_MIN) -> dict:
    """タイムライングリッドのコンテキスト。

    columns: [{"channel": Channel, "programs": [Program,...], "rerun_segments": [...]}, ...]
    (programs は当日窓・start_at 昇順。rerun_segments は任意=フィラー由来の再放送ブロック)。
    """
    day_start = _day_start(base_date)
    height = DAY_MIN * px_per_min
    hours = [
        {
            "label": f"{h:02d}:00",
            "top_px": f"{int(h * 60 * px_per_min)}px",
            "top": int(h * 60 * px_per_min),
        }
        for h in range(24)
    ]
    now_min = _minutes(now, day_start)
    show_now = 0.0 <= now_min <= DAY_MIN

    cols = []
    for col in columns:
        ch = col["channel"]
        tint = ch.tint_color
        blocks = []
        for p in col["programs"]:
            is_live = show_now and p.start_at <= now < p.end_at
            blk = _build_block(p, day_start, tint, is_live, px_per_min)
            if blk:
                blocks.append(blk)
        for seg in col.get("rerun_segments", []):
            is_live = show_now and seg.start_at <= now < seg.end_at
            blk = _build_block(seg, day_start, tint, is_live, px_per_min, is_rerun=True)
            if blk:
                blocks.append(blk)
        blocks.sort(key=lambda b: b["top"])  # 編成 + 再放送を時刻順に整列
        cols.append(
            {
                "slug": ch.slug,
                "name": ch.name,
                "short": ch.short_name,
                "tint": tint,
                "blocks": blocks,
            }
        )

    return {
        "px_per_min": px_per_min,  # client ズームの初期密度 / リセット先
        "height_px": f"{height:.0f}px",
        "height": round(height, 1),
        "hours": hours,
        "cols": cols,
        "now_top_px": f"{now_min * px_per_min:.1f}px",
        "now_top": round(now_min * px_per_min, 1) if show_now else None,
        "now_min": round(now_min, 2) if show_now else None,  # client がズームで now ラインを再計算
        "now_label": f"{timezone.localtime(now):%H:%M}",
        "show_now": show_now,
    }


def day_list(channel, programs, now, *, limit: int = 9, rerun_segments=None) -> list[dict]:
    """プレイヤー右「本日の番組」リスト (デザインの dayList)。現在以降を上から limit 件。

    rerun_segments (任意) はフィラー由来の再放送行。編成 Program と時刻順にマージして出す。
    """
    tint = channel.tint_color
    items = [(p, False) for p in programs] + [(s, True) for s in (rerun_segments or [])]
    items.sort(key=lambda it: it[0].start_at)
    rows = []
    for p, is_rerun in items:
        if p.end_at <= now:
            continue  # 終了済みは出さない (現在 + これから)
        is_now = p.start_at <= now < p.end_at
        color = genre_color(p.resolved_genre) or (_RERUN_BORDER if is_rerun else tint)
        rows.append(
            {
                "id": p.id,
                "time": f"{timezone.localtime(p.start_at):%H:%M}",
                "title": p.title,
                "genre": p.resolved_genre,
                "color": color,
                "is_now": is_now,
                "is_rerun": is_rerun,
                "row_bg": "rgba(56,189,248,.07)" if is_now else "transparent",
                "time_color": "#eef1f7" if is_now else "#8b93a7",
                "title_color": "#eef1f7" if is_now else "#aeb6c9",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def build_week_epg(
    channel, days, programs_by_day, now, *, px_per_min: float = PX_PER_MIN, rerun_by_day=None
) -> dict:
    """週間グリッド (1ch × 7日)。時間軸 00:00–24:00 を共通縦軸とし、列 = 各日。

    days: [date,...] (昇順 7 日)。programs_by_day: {date: [Program,...]} (各日窓に掛かる public program)。
    rerun_by_day (任意): {date: [RerunSegment,...]} = フィラー由来の再放送ブロック。
    日跨ぎ番組は各日窓へクランプ。now-line / ON AIR 強調は「今日」の列のみ。
    日(全ch)グリッド build_epg と同じ pxPerMin / ブロック幾何を共有する。
    """
    height = DAY_MIN * px_per_min
    hours = [{"label": f"{h:02d}:00", "top_px": f"{int(h * 60 * px_per_min)}px"} for h in range(24)]
    today = timezone.localdate()
    tint = channel.tint_color
    rerun_by_day = rerun_by_day or {}
    cols = []
    for d in days:
        day_start = _day_start(d)
        is_today = d == today
        blocks = []
        for p in programs_by_day.get(d, []):
            is_live = is_today and p.start_at <= now < p.end_at
            blk = _build_block(p, day_start, tint, is_live, px_per_min)
            if blk:
                blocks.append(blk)
        for seg in rerun_by_day.get(d, []):
            is_live = is_today and seg.start_at <= now < seg.end_at
            blk = _build_block(seg, day_start, tint, is_live, px_per_min, is_rerun=True)
            if blk:
                blocks.append(blk)
        blocks.sort(key=lambda b: b["top"])  # 編成 + 再放送を時刻順に整列
        now_min = _minutes(now, day_start)
        col_now = is_today and 0.0 <= now_min <= DAY_MIN
        cols.append(
            {
                "date": d,
                "weekday": _WEEKDAY_JA[d.weekday()],
                "label": f"{d.month}/{d.day}",
                "is_today": is_today,
                "is_weekend": d.weekday() >= 5,
                "blocks": blocks,
                "now_top_px": (f"{now_min * px_per_min:.1f}px" if col_now else None),
                "now_min": round(now_min, 2)
                if col_now
                else None,  # client ズームの now ライン再計算用
            }
        )
    today_now = next((c["now_top_px"] for c in cols if c["is_today"]), None)
    today_now_min = next((c["now_min"] for c in cols if c["is_today"]), None)
    return {
        "px_per_min": px_per_min,  # client ズームの初期密度 / リセット先
        "height_px": f"{height:.0f}px",
        "hours": hours,
        "cols": cols,
        "tint": tint,
        "now_label": f"{timezone.localtime(now):%H:%M}",
        "now_top_px": today_now,
        "now_min": today_now_min,  # client がズームで自動スクロール位置を再計算
        "show_now": today_now is not None,
    }


def cross_upcoming(columns, now, *, limit: int = 8, per_channel: int = 3) -> list[dict]:
    """ホーム「これからの番組」横スクロール。全 ch の次番組以降を時刻順に limit 件 (デザインの up)。"""
    items = []
    for col in columns:
        ch = col["channel"]
        nexts = [p for p in col["programs"] if p.start_at > now][:per_channel]
        for p in nexts:
            color = genre_color(p.resolved_genre) or ch.tint_color
            items.append(
                {
                    "id": p.id,
                    "start": p.start_at,
                    "time": f"{timezone.localtime(p.start_at):%H:%M}",
                    "title": p.title,
                    "genre": p.resolved_genre,
                    "color": color,
                    "ch_short": ch.short_name,
                    "ch_tint": ch.tint_color,
                }
            )
    items.sort(key=lambda x: x["start"])
    return items[:limit]
