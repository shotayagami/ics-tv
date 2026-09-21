# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""delivery アプリは別リポ ICS-DELIVERY サービスへ完全移管した (リファクタ Phase 3.9 Stage G/H)。

旧 7 モデル (production_company / delivery_account / delivery_invitation / delivery /
delivery_file / qc_report / sheet_export) は migration 0006 で DROP した。本アプリはモデルを
持たない「ghost」として INSTALLED_APPS / migrations に残す: procurement の歴史的 migration が
delivery の migration を依存に持つため (削除すると NodeNotFoundError)。

ICS-TV との連携は内部 seam (api/routers/internal.py の /internal/delivery-asset・/delivery-refs)
が担い、本アプリのコードには一切依存しない。納品ポータル/QC/正規化は ICS-DELIVERY サービスが所有。
"""

from __future__ import annotations
