# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開フロント用テンプレートフィルタ (#7 デザイン刷新)。

`{% load icstv_public %}` してジャンル色などを解決する。色マップは core.genre に一元化。
"""

from __future__ import annotations

from django import template

from core import genre as genre_mod

register = template.Library()


@register.filter(name="genre_color")
def genre_color(value: str | None) -> str:
    """ジャンル名 → hex 色 ("" は未設定/未知)。"""
    return genre_mod.genre_color(value)


# 通貨ごとの表示規則。Stripe の zero-decimal 通貨は minor unit がそのまま主単位になる
# (JPY は amount=1000 が ¥1,000)。2-decimal 通貨は 100 で割る (USD は amount=1000 が $10.00)。
# 対応通貨が増えたらここに足す。未知の通貨は記号を付けず ISO コードを併記して、
# **誤った記号で表示するより「読めない」ほうを選ぶ** (金額の誤認を避けるため)。
_ZERO_DECIMAL = {"jpy", "krw", "vnd", "clp", "isk"}
_SYMBOL = {"jpy": "¥", "usd": "$", "eur": "€", "gbp": "£"}


@register.filter(name="money")
def money(amount_minor, currency: str = "jpy") -> str:
    """minor unit の金額を通貨に応じて整形する。

    使い方: `{{ row.total_net|money:row.currency }}`

    金額カラムを `*_jpy` から `*_minor` + `currency` へ移したことで、テンプレート側の
    `¥` ハードコードが実体と食い違いうるようになったため、表示を 1 箇所へ集約する。
    """
    if amount_minor is None:
        amount_minor = 0
    cur = (currency or "jpy").lower()
    body = f"{int(amount_minor):,}" if cur in _ZERO_DECIMAL else f"{int(amount_minor) / 100:,.2f}"
    sym = _SYMBOL.get(cur)
    return f"{sym}{body}" if sym else f"{body} {cur.upper()}"
