# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SeriesPost のファンクラブ限定ゲート (#27)。SSR (core.views) + JSON API (series_public) 両方。"""

from __future__ import annotations

import json

from django.test import override_settings

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorSeriesLink
from members.models import Member
from scheduling.models import Series, SeriesPost, SeriesPostKind

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a"):
    return Creator.objects.create(name="サークルA", slug=slug)


def _series(channel, creator=None, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    if creator is not None:
        CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


def _post(series, *, fc_required_level=None, body="ここに秘密の本文があります"):
    return (
        SeriesPost.objects.create(
            series=series,
            kind=SeriesPostKind.ARTICLE,
            title="お知らせ",
            body=body,
            is_published=True,
        )
        if fc_required_level is None
        else SeriesPost.objects.create(
            series=series,
            kind=SeriesPostKind.ARTICLE,
            title="限定お知らせ",
            body=body,
            is_published=True,
            fc_required_level=fc_required_level,
        )
    )


# ---- SSR (public_series_post) ----


@_PUBLIC_HOST
def test_ssr_unlocked_post_shows_body(http_client, channel, db):
    s = _series(channel)
    post = _post(s, fc_required_level=None)
    res = http_client.get(f"/series/{s.id}/p/{post.id}/")
    assert res.status_code == 200
    assert "ここに秘密の本文があります" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_ssr_locked_post_hides_body_anonymous(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    post = _post(s, fc_required_level=0)
    res = http_client.get(f"/series/{s.id}/p/{post.id}/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "ここに秘密の本文があります" not in body
    assert "ファンクラブ限定" in body


@_PUBLIC_HOST
def test_ssr_locked_post_hides_body_when_not_joined(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    post = _post(s, fc_required_level=0)
    _member()
    _login(http_client)
    res = http_client.get(f"/series/{s.id}/p/{post.id}/")
    body = res.content.decode("utf-8")
    assert "ここに秘密の本文があります" not in body


@_PUBLIC_HOST
def test_ssr_locked_post_shows_body_after_join(http_client, channel, db):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    post = _post(s, fc_required_level=0)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)
    res = http_client.get(f"/series/{s.id}/p/{post.id}/")
    assert "ここに秘密の本文があります" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_ssr_locked_post_og_description_not_leaked(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    post = _post(s, fc_required_level=0)
    res = http_client.get(f"/series/{s.id}/p/{post.id}/")
    body = res.content.decode("utf-8")
    assert "ここに秘密の本文があります" not in body  # OG description にも含まれない


@_PUBLIC_HOST
def test_ssr_unlinked_series_post_never_gated(http_client, channel, db):
    """creator 未紐付けの Series は既存 SeriesPost の挙動を一切変えない (非破壊確認)。"""
    s = _series(channel, creator=None)
    post = _post(s, fc_required_level=None)
    res = http_client.get(f"/series/{s.id}/p/{post.id}/")
    assert "ここに秘密の本文があります" in res.content.decode("utf-8")


# ---- JSON API (GET /api/v1/series/{id}) ----


@_PUBLIC_HOST
def test_api_locked_flag_present(http_client, channel, db):
    c = _creator()
    fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    post = _post(s, fc_required_level=0)
    data = json.loads(http_client.get(f"/api/v1/series/{s.id}").content)
    item = next(p for p in data["posts"] if p["id"] == post.id)
    assert item["locked"] is True
    assert item["gate_reason"] == "login"
    assert item["title"] == "限定お知らせ"  # タイトルは隠さない (ロック表示方針)


@_PUBLIC_HOST
def test_api_unlocked_flag_for_existing_posts(http_client, channel, db):
    """既存 (fc_required_level=NULL) の投稿は locked=False のまま (非破壊確認)。"""
    s = _series(channel)
    post = _post(s, fc_required_level=None)
    data = json.loads(http_client.get(f"/api/v1/series/{s.id}").content)
    item = next(p for p in data["posts"] if p["id"] == post.id)
    assert item["locked"] is False
    assert item["gate_reason"] == ""


@_PUBLIC_HOST
def test_api_locked_becomes_false_after_join(http_client, channel, db):
    c = _creator()
    tier = fc_services.ensure_free_tier(c)
    s = _series(channel, creator=c)
    post = _post(s, fc_required_level=0)
    m = _member()
    fc_services.join(m, c, tier)
    _login(http_client)
    data = json.loads(http_client.get(f"/api/v1/series/{s.id}").content)
    item = next(p for p in data["posts"] if p["id"] == post.id)
    assert item["locked"] is False
