# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員状態をテンプレへ供給 (公開ナビの ログイン/マイページ・未認証バナー)。

request.member は MemberAuthMiddleware が遅延付与した SimpleLazyObject。テンプレが
member を参照しなければ DB は引かれない。
"""

from __future__ import annotations


def member(request) -> dict:
    return {"member": getattr(request, "member", None)}
