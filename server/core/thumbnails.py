# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サムネ画像の R2 保存 + 公開配信パス生成 (#7 Phase 2)。

URL 貼り付けに加え、画像ファイルを R2 (thumbnails/ prefix) へアップロードして
アプリ経由 (/t/<key>) で公開配信する。CF 公開バケット設定なしで安定 URL を得るため app 配信。
"""

from __future__ import annotations

import uuid

from core import r2

THUMB_PREFIX = "thumbnails/"
MAX_BYTES = 5 * 1024 * 1024  # 5MB
_ALLOWED = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class ThumbnailError(ValueError):
    pass


def store(django_file) -> str:
    """アップロードファイルを R2 に保存し、配信パス (/t/<key>) を返す。"""
    content_type = (getattr(django_file, "content_type", "") or "").lower()
    ext = _ALLOWED.get(content_type)
    if not ext:
        raise ThumbnailError("画像は JPEG / PNG / WebP / GIF のみ対応です")
    if django_file.size and django_file.size > MAX_BYTES:
        raise ThumbnailError("画像サイズは 5MB 以内にしてください")
    key = f"{THUMB_PREFIX}{uuid.uuid4().hex}.{ext}"
    r2.put_object(key, django_file.read(), content_type)
    return "/t/" + key


def apply_upload(request, field_name: str = "thumbnail_file") -> str | None:
    """request.FILES に画像があれば R2 保存して配信 URL を返す。無ければ None。"""
    f = request.FILES.get(field_name)
    if not f:
        return None
    return store(f)
