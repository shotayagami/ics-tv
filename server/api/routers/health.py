# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ヘルス/メタ。Phase 0 の疎通確認 + OpenAPI/TS クライアント生成の最小例。"""

from django.conf import settings
from django.http import HttpRequest
from ninja import Router

from api.schemas import HealthOut

router = Router(tags=["meta"])


@router.get("/", response=HealthOut, auth=None)
def health(request: HttpRequest) -> HealthOut:
    """API 稼働確認。ビルド時に焼き込んだ版数 (ICSTV_VERSION) を返す。"""
    return HealthOut(status="ok", service="icstv-api", version=settings.ICSTV_VERSION)
