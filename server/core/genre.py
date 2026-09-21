# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ジャンル → 表示色のマッピング (#7 公開フロント刷新)。

色は design_handoff (ICS-TV.dc.html の genreColor map) に一致させる。チップ/枠線/EPG ブロックの
差し色として公開テンプレ + ビューで共用する。未設定/未知ジャンルは空文字を返し、呼び出し側で
チップ非表示・枠線は ch 識別色 (Channel.tint_color) へフォールバックさせる。
"""

from __future__ import annotations

GENRE_COLORS: dict[str, str] = {
    "ニュース": "#457b9d",
    "情報": "#457b9d",
    "アニメ": "#7209b7",
    "映画": "#6a4c93",
    "音楽": "#1982c4",
    "ゲーム": "#2a9d8f",
    "バラエティ": "#f4a261",
    "教養": "#2d6a4f",
    "文化": "#2d6a4f",
    "科学": "#2d6a4f",
    "技術": "#2d6a4f",
    "語学": "#2d6a4f",
    "ドキュメンタリー": "#2d6a4f",
}


def genre_color(genre: str | None) -> str:
    """ジャンル名 → hex 色。未設定/未知は "" (チップ非表示・枠線は ch tint にフォールバック)。"""
    if not genre:
        return ""
    return GENRE_COLORS.get(genre, "")
