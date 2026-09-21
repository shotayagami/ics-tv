# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""core.sheets.to_csv (リファクタ Phase 3.9 で delivery.sheets から移設) の純関数テスト。"""

from __future__ import annotations

import pytest

from core.sheets import escape_formula, to_csv


def test_to_csv_header_and_rows():
    cols = [("名前", lambda r: r["name"]), ("数", lambda r: r["n"])]
    rows = [{"name": "a", "n": 1}, {"name": "b", "n": 2}]
    out = to_csv(rows, cols)
    lines = out.splitlines()
    assert lines[0] == "名前,数"
    assert lines[1] == "a,1"
    assert lines[2] == "b,2"


def test_to_csv_empty_rows_keeps_header():
    out = to_csv([], [("x", lambda r: r["x"])])
    assert out.strip() == "x"


# --------------------------------------------------------------------------- #
#  数式インジェクション無害化 (#sec L-9)                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        '=HYPERLINK("https://evil.example","click")',
        "+1+1",
        "-2+3",
        "@SUM(A1:A9)",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_escape_formula_prefixes_dangerous_strings(raw):
    """表計算が数式として評価しうる先頭文字は ' で無害化する。"""
    assert escape_formula(raw) == "'" + raw


@pytest.mark.parametrize("raw", ["ふつうの番組名", "a=b", "", "1+1", "https://example.com"])
def test_escape_formula_leaves_safe_strings_untouched(raw):
    """先頭が危険文字でなければ触らない (= が途中にあるだけの値を壊さない)。"""
    assert escape_formula(raw) == raw


@pytest.mark.parametrize("raw", [1, 0, -5, 3.5, None, True])
def test_escape_formula_passes_through_non_strings(raw):
    """数値・None はそのまま返す。' を付けると表計算側で数値でなくなる。"""
    assert escape_formula(raw) is raw


def test_to_csv_neutralises_formula_in_values():
    """to_csv を通した時点で無害化されている (呼び出し側の対応を要求しない)。"""
    cols = [("名前", lambda r: r["name"])]
    out = to_csv([{"name": "=1+1"}], cols)
    lines = out.splitlines()
    assert lines[0] == "名前"
    # csv.writer は先頭 ' を含む値をそのまま書く (引用符付けの対象文字ではない)。
    assert lines[1] == "'=1+1"


def test_to_csv_does_not_touch_header_names():
    """ヘッダは運用者が定義した列名なので無害化の対象外。"""
    out = to_csv([], [("=総数", lambda r: "")])
    assert out.splitlines()[0] == "=総数"
