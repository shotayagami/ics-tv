# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開トップの「いま放送中」ライブ静止画 (YouTube カード相当) の R2 保存/配信。

送出ノードの poster-grab (deploy/playout-node/scripts/poster-grab.sh) が MediaMTX の
現在フレームを ~10s おきに JPEG 化し、管理ホストの ingest エンドポイントへ POST する。
サーバはそれを R2 (live/<slug>.jpg) に上書き保存し、公開ホストの配信 view で短期キャッシュ付き
で返す。pod 間で共有が要るため Django cache (既定 LocMemCache=pod ローカル) ではなく R2 を使う。
"""

from __future__ import annotations

from core import r2

R2_PREFIX = "live/"
MAX_BYTES = 2 * 1024 * 1024  # 2MB (640px JPEG なら十分。過大 body を弾く)
# 配信キャッシュ秒。クライアントは ?ts=floor(now/SERVE_TTL) でバケット化するため、
# 視聴者が増えても CF/ブラウザがバケット単位でキャッシュし origin/R2 read は ~1/SERVE_TTL に収束する。
SERVE_TTL = 10


def r2_key(slug: str) -> str:
    return f"{R2_PREFIX}{slug}.jpg"


def put(slug: str, body: bytes) -> None:
    r2.put_object(r2_key(slug), body, "image/jpeg")


def get(slug: str) -> bytes:
    body, _ = r2.get_object(r2_key(slug))
    return body
