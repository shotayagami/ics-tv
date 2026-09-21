# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サムネの R2 アップロード + アプリ経由配信 (#7 Phase 2)。R2 はモックする。"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core import r2, thumbnails


def test_store_uploads_image_and_returns_serve_path(monkeypatch):
    seen = {}

    def fake_put(key, body, content_type):
        seen.update(key=key, content_type=content_type, size=len(body))

    monkeypatch.setattr(r2, "put_object", fake_put)
    f = SimpleUploadedFile("pic.png", b"\x89PNG\r\n\x1a\n data", content_type="image/png")
    url = thumbnails.store(f)
    assert url.startswith("/t/thumbnails/") and url.endswith(".png")
    assert seen["content_type"] == "image/png" and seen["size"] > 0


def test_store_rejects_non_image(monkeypatch):
    monkeypatch.setattr(r2, "put_object", lambda *a, **k: None)
    f = SimpleUploadedFile("x.txt", b"hello", content_type="text/plain")
    with pytest.raises(thumbnails.ThumbnailError):
        thumbnails.store(f)


def test_store_rejects_oversize(monkeypatch):
    monkeypatch.setattr(r2, "put_object", lambda *a, **k: None)
    big = SimpleUploadedFile("big.png", b"x" * 10, content_type="image/png")
    big.size = thumbnails.MAX_BYTES + 1  # 実バイト読まずにサイズだけ偽装
    with pytest.raises(thumbnails.ThumbnailError):
        thumbnails.store(big)


def test_thumb_serve_streams_with_cache(client, monkeypatch):
    monkeypatch.setattr(r2, "get_object", lambda key: (b"IMGBYTES", "image/webp"))
    res = client.get("/t/thumbnails/abc123.webp")
    assert res.status_code == 200
    assert res["Content-Type"] == "image/webp"
    assert "max-age" in res["Cache-Control"]
    assert res.content == b"IMGBYTES"


def test_thumb_serve_rejects_non_thumbnail_prefix(client):
    res = client.get("/t/secret/passwd")
    assert res.status_code == 404


def test_asset_edit_file_upload_sets_thumbnail(staff_client, db, monkeypatch):
    from medialib.models import Asset, AssetKind

    monkeypatch.setattr(r2, "put_object", lambda *a, **k: None)
    a = Asset.objects.create(kind=AssetKind.PROGRAM, title="番組")
    f = SimpleUploadedFile("t.png", b"\x89PNGdata", content_type="image/png")
    res = staff_client.post(
        f"/medialib/asset/{a.id}/edit/",
        {
            "kind": "program",
            "title": "番組",
            "thumbnail_url": "",
            "r2_key": "",
            "source_path": "",
            "thumbnail_file": f,
        },
    )
    assert res.status_code == 302
    a.refresh_from_db()
    assert a.thumbnail_url and a.thumbnail_url.startswith("/t/thumbnails/")
