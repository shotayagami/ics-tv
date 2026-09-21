# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Series スラッグ — URL 解決・後方互換・自動採番。"""

from __future__ import annotations

import pytest
from django.test import override_settings

from scheduling.models import Series

# 公開 urlconf は middleware が ICSTV_ADMIN_HOSTS 非一致ホストで切替えるため、
# テストでは ICSTV_ADMIN_HOSTS=[] にして公開ルートへ倒す (test_public_redesign.py 流儀)。
_PUBLIC = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


def _make_series(channel, title, slug=""):
    return Series.objects.create(channel=channel, title=title, slug=slug)


# ---------- slugify_unique ----------


def test_slugify_unique_basic(channel, db):
    from scheduling.services import slugify_unique

    # 日本語タイトルは slugify で空になるため既定の "program" にフォールバック
    s = slugify_unique("モーニングショー", channel.id)
    assert s == "program"
    # 英語タイトルは ASCII slug に
    s2 = slugify_unique("Morning News", channel.id)
    assert s2 == "morning-news"


def test_slugify_unique_collision(channel, db):
    from scheduling.services import slugify_unique

    Series.objects.create(channel=channel, title="Morning News", slug="morning-news")
    s = slugify_unique("Morning News", channel.id)
    assert s == "morning-news-2"


def test_slugify_unique_excludes_self(channel, db):
    from scheduling.services import slugify_unique

    existing = Series.objects.create(channel=channel, title="Test Show", slug="test-show")
    # 自分自身は衝突カウントから除外されるので同じ slug が返る
    s = slugify_unique("Test Show", channel.id, exclude_series_id=existing.id)
    assert s == "test-show"


def test_slugify_unique_cross_channel_no_collision(channel, db):
    """別チャンネルは一意制約の対象外。"""
    from core.models import Channel
    from scheduling.services import slugify_unique

    ch2 = Channel.objects.create(name="ch2", slug="ch2", enabled=True)
    Series.objects.create(channel=ch2, title="Morning News", slug="morning-news")
    s = slugify_unique("Morning News", channel.id)
    assert s == "morning-news"  # 同名でも別 ch なので衝突しない


def test_slugify_unique_rejects_digit_only(channel, db):
    from scheduling.services import slugify_unique

    # 数字のみの title は digit-only → "program" にフォールバック
    s = slugify_unique("123", channel.id)
    assert s == "program"


# ---------- slug URL ルーティング ----------


@_PUBLIC
def test_slug_url_resolves(channel, db, client):
    """公開 /series/<slug>/ が 200 を返す。"""
    Series.objects.create(channel=channel, title="Test Show", slug="test-show")
    resp = client.get("/series/test-show/")
    assert resp.status_code == 200


@_PUBLIC
def test_id_url_backward_compat(channel, db, client):
    """slug なし (ID URL) でも 200 を返す。"""
    s = Series.objects.create(channel=channel, title="No Slug Series")
    resp = client.get(f"/series/{s.id}/")
    assert resp.status_code == 200


@_PUBLIC
def test_id_url_redirects_to_slug_when_set(channel, db, client):
    """slug 設定済みの series は ID URL → slug URL へ 301 リダイレクト。"""
    s = Series.objects.create(channel=channel, title="Has Slug", slug="has-slug")
    resp = client.get(f"/series/{s.id}/")
    assert resp.status_code == 301
    assert resp["Location"].endswith("/series/has-slug/")


@_PUBLIC
def test_slug_url_unknown_404(channel, db, client):
    resp = client.get("/series/does-not-exist/")
    assert resp.status_code == 404


# ---------- UniqueConstraint ----------


def test_empty_slug_allows_multiple(channel, db):
    """空 slug は複数作成できる (制約対象外)。"""
    Series.objects.create(channel=channel, title="A", slug="")
    Series.objects.create(channel=channel, title="B", slug="")  # should not raise


def test_duplicate_slug_same_channel_raises(channel, db):
    from django.db import IntegrityError

    Series.objects.create(channel=channel, title="X", slug="dupe")
    with pytest.raises(IntegrityError):
        Series.objects.create(channel=channel, title="Y", slug="dupe")


# ---------- Series properties ----------


def test_series_x_properties(channel, db):
    s = Series.objects.create(
        channel=channel, title="T", x_handle="icstv_test", x_hashtag="ICSTest"
    )
    assert s.x_handle_display == "@icstv_test"
    assert "x.com/icstv_test" in s.x_url
    assert s.x_hashtag_display == "#ICSTest"
    assert "ICSTest" in s.hashtag_url


def test_series_x_properties_empty(channel, db):
    s = Series.objects.create(channel=channel, title="T")
    assert s.x_handle_display == ""
    assert s.x_url == ""
    assert s.x_hashtag_display == ""
    assert s.hashtag_url == ""
