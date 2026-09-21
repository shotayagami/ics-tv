# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""視聴年齢制限ゲート (#BILL-02)。

会員登録で収集済みの生年月から満年齢 (Member.age) を出し、番組の resolved_rating が
要求する min_age と突き合わせる。リニア (共有配信) は視聴者別にブロックできないため、
ハードゲートは自前再生制御が効く VOD (scheduling.vod の can_watch/gate_reason) に結線する。
番組詳細等はレーティングバッジ表示で補う。
"""

from __future__ import annotations


def age_gate_reason(program, member, now=None) -> str:
    """年齢制限の判定結果を返す。

    - ''      : 視聴可 (制限なし or 年齢を満たす)
    - 'login' : 年齢不明 (未ログイン) のため要ログイン
    - 'age'   : ログイン済みだが年齢不足
    """
    min_age = program.min_age
    if min_age <= 0:
        return ""
    # 匿名は MemberAuthMiddleware が None を包む SimpleLazyObject なので truthy 判定する
    # (`is None` では素通りして member.age() で AttributeError になる)。
    if not member:
        return "login"
    return "" if member.age(now) >= min_age else "age"
