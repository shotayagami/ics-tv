# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員統計の共通集計。

staff 画面 (members.staff_views.stats) と studio SPA 向け API
(api.routers.admin.members_stats) が同じ数字を出すための置き場。両者は同じ集計を
別々に書いていたため、国別の扱いのように後から入る条件が片方だけに入る形になりやすい。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from members.models import COUNTRY_NAMES


# 引数は Mapping/Sequence で受ける。呼び出し側は QuerySet.values() の戻り (django-stubs では
# TypedDict の list) をそのまま渡すため、list[dict] だと不変性で型が合わない。
def split_by_country(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], int, list[tuple[str, int]]]:
    """`(JP の行, 海外の件数, 国内訳 [(表示名, 件数)])` を返す。

    地域内訳は郵便番号の上 3 桁で集計するが、これは**日本の郵便番号の体系に依存**する。
    海外の郵便番号 (SW1A 1AA 等) を同じ棚に入れると、日本の県コードと文字列が混ざって
    意味を成さない。よって地域は JP のみを母数にし、除外した件数を併記して母数のズレを見せる。
    """
    jp_rows = [r for r in rows if (r.get("country") or "JP") == "JP"]
    counts = Counter((r.get("country") or "JP") for r in rows)
    country_rows = [
        (COUNTRY_NAMES.get(code, code), n)
        for code, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return jp_rows, len(rows) - len(jp_rows), country_rows
