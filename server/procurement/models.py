# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""番組予算 (procurement) は追加提供側へ移した (P4 第 4 単位・D037)。

旧 3 モデル (program_budget / delivery_cost / procurement_payment) は migration 0004 で DROP した。
本アプリはモデルを持たない「殻」として INSTALLED_APPS / migrations に残す。既存の migration を
1 本も消さない方式を採っているため (D037)、delivery/0006 が procurement/0002 を依存に持つ関係も
そのまま保たれる。

番組予算の画面・API・集計は追加提供側にある。このツリーに残るのは
`GET /api/v1/internal/backoffice/budget` と `/picker` の口だけで、どちらも常に空を返す (D036)。
"""

from __future__ import annotations
