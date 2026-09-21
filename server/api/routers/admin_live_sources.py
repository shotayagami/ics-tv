# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA: 生入力 LiveSource (OBS ingest 接続情報の閲覧)。staff 限定。

現場 OBS/サブ卓へ渡す SRT/RTMP ingest URL を studio で確認できるようにする。これまで
ingest URL ヒントは Django admin (core.admin) の readonly field でしか見られず、現場配布の
たびに admin を開く必要があった。CRUD (LiveSource の作成/編集/passphrase 設定) は外部運用の
都合で当面 Django admin に据え置き、本 API は閲覧 (URL 生成 + コピー) に徹する (admin_url で誘導)。

host は LiveSource モデルのデフォルト引数 (プレースホルダ) ではなく settings.ICSTV_INGEST_NODE_HOST
を渡して送出ノードの実 IP (例 192.0.2.10) を埋める。未設定なら雛形のまま返し host_configured=False
を立てて画面で警告する。
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from ninja import Router

from api.auth import staff_auth
from api.schemas import LiveSourcesOut

router = Router(tags=["admin"], auth=staff_auth)

# host 未設定時に LiveSource.*_ingest_url が使う雛形 (models.py のデフォルト引数と同値)。
_PLACEHOLDER_HOST = "<送出ノード private IP>"


@router.get("/admin/live-sources", response=LiveSourcesOut)
def live_sources(request: HttpRequest):
    from core.models import LiveSource

    host = settings.ICSTV_INGEST_NODE_HOST or _PLACEHOLDER_HOST
    sources = [
        {
            "id": s.id,
            "name": s.name,
            "mediamtx_path": s.mediamtx_path,
            "srt_latency_ms": s.srt_latency_ms,
            "has_passphrase": bool(s.srt_passphrase),
            "srt_url": s.srt_ingest_url(host),
            "rtmp_url": s.rtmp_ingest_url(host),
            "note": s.note or "",
        }
        for s in LiveSource.objects.order_by("name")
    ]
    return {
        "ingest_host": host,
        "host_configured": bool(settings.ICSTV_INGEST_NODE_HOST),
        "admin_url": "/admin/core/livesource/",
        "sources": sources,
    }
