# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* テンプレへ request.creator_account を配る (members.context_processors.member と同型)。"""

from __future__ import annotations


def creator_account(request) -> dict:
    return {"creator_account": getattr(request, "creator_account", None)}
