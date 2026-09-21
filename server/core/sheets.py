# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""汎用 CSV 生成ヘルパー (リファクタ Phase 3.9 で delivery.sheets から移設)。

cols を [(ヘッダ名, row->値 の関数), ...] とし、rows を CSV 文字列にする純関数。
現在のツリーでの利用実態: escape_formula は api/routers/admin_series.py の公開フォーム投稿の
CSV 書き出しが使う。to_csv を呼ぶのはテスト (tests/api/test_core_sheets.py) だけだが、
テストのある汎用ヘルパーとして残す。移設元の delivery/sheets.py は現存しない
(delivery アプリ側の同等関数は撤去済み)。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable

# 表計算ソフトが「数式」として解釈し始める先頭文字 (#sec L-9 数式インジェクション)。
# = と + と - は Excel/LibreOffice/Sheets 共通、@ は Excel の古い関数呼び出し記法。
# タブと復帰は前置されると表計算側で剥がされ、後ろの = が先頭に繰り上がるため同列に扱う。
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def escape_formula(value: object) -> object:
    """表計算の数式として解釈されうる文字列の先頭に ' を付けて無害化する。

    CSV 自体は仕様どおりでも、受け手が Excel/LibreOffice/Google Sheets で開くと
    先頭が = の値は数式として評価される。値の出所が利用者入力 (公開フォームの投稿者名・
    自由入力欄など) である以上、`=HYPERLINK(...)` や `=cmd|...` を仕込まれると閲覧者側で
    情報が抜かれうる。

    文字列以外 (int / date / None など) はそのまま返す。表計算側が数式と誤読するのは
    文字列として書き出される値だけで、数値に ' を付けると今度は数値として扱われなくなる。
    """
    if not isinstance(value, str) or not value.startswith(_FORMULA_TRIGGERS):
        return value
    return "'" + value


def to_csv(rows: list[dict], cols: list[tuple[str, Callable[[dict], object]]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([name for name, _ in cols])
    for r in rows:
        # 数式インジェクション対策はここに一点だけ置く (#sec L-9)。呼び出し側の
        # 各カラム関数に散らすと、カラムを足したときに必ず抜ける。
        w.writerow([escape_formula(fn(r)) for _, fn in cols])
    return buf.getvalue()
