# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OGP / Twitter カード (#COMM-02): 共通メタ + 番組詳細での上書き・og:image。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from scheduling.models import Program, ProgramType, Series

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _program(channel, asset, *, title="番組X", series=None, start_delta=timedelta(hours=3)):
    start = timezone.now() + start_delta
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=start,
        end_at=start + timedelta(hours=1),
        asset=asset,
        series=series,
    )


@_PUBLIC_HOST
def test_base_og_tags_present(http_client, channel, db):
    body = http_client.get("/").content.decode("utf-8")
    assert 'property="og:site_name" content="ICS-TV"' in body
    assert 'property="og:type" content="website"' in body
    assert 'name="twitter:card"' in body


@_PUBLIC_HOST
def test_program_og_overrides(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready, title="特集ドラマ")
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert 'property="og:type" content="video.other"' in body
    assert 'property="og:title" content="特集ドラマ — ICS-TV"' in body


@_PUBLIC_HOST
def test_program_og_image_absolute_when_thumb(http_client, channel, asset_ready, db):
    s = Series.objects.create(
        channel=channel, title="連続", thumbnail_url="https://cdn.example/t.jpg"
    )
    p = _program(channel, asset_ready, series=s)
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert 'property="og:image" content="https://cdn.example/t.jpg"' in body
    assert 'name="twitter:card" content="summary_large_image"' in body


@_PUBLIC_HOST
def test_program_no_og_image_without_thumb(http_client, channel, asset_ready, db):
    p = _program(channel, asset_ready)  # asset_ready/series にサムネなし
    body = http_client.get(f"/program/{p.id}/").content.decode("utf-8")
    assert "og:image" not in body
    assert 'name="twitter:card" content="summary"' in body
