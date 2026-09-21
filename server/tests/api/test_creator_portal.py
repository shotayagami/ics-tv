# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* セルフサービス画面 (#27): dashboard/ティア/番組/投稿CRUD/会員集計/枠契約閲覧。

テナント境界(他クリエイターのSeriesは404)と、fc_required_levelのSSRフォーム経由設定を重点確認。
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorAccount, CreatorSeriesLink, CreatorTier, SlotContract
from scheduling.models import Series, SeriesPost

CREATOR_HOST = "creator.example.com"
_HOSTS = override_settings(
    ALLOWED_HOSTS=["testserver", CREATOR_HOST],
    ICSTV_ADMIN_HOSTS=[],
    ICSTV_CREATOR_HOSTS=[CREATOR_HOST],
)


@pytest.fixture
def web():
    return Client()


def _creator(slug="circle-a"):
    c = Creator.objects.create(name="サークルA", slug=slug)
    fc_services.ensure_free_tier(c)
    return c


def _account(creator, email="me@example.com"):
    return CreatorAccount.objects.create(creator=creator, email=email, google_sub="sub-1")


def _login_as(web, account):
    session = web.session
    session["creator_account_id"] = account.pk
    session.save()


def _series(channel, creator=None, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    if creator is not None:
        CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


# ---- dashboard ----


@_HOSTS
def test_dashboard_requires_login(web, db):
    res = web.get("/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url.startswith("/login/")


@_HOSTS
def test_dashboard_shows_counts(web, channel, db):
    c = _creator()
    _series(channel, creator=c)
    account = _account(c)
    _login_as(web, account)
    res = web.get("/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 200
    assert "サークルA" in res.content.decode("utf-8")


# ---- tiers ----


@_HOSTS
def test_tiers_list_shows_free_tier(web, db):
    c = _creator()
    account = _account(c)
    _login_as(web, account)
    res = web.get("/tiers/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 200
    assert "常設" in res.content.decode("utf-8")


@_HOSTS
def test_tier_create_paid(web, db):
    c = _creator()
    account = _account(c)
    _login_as(web, account)
    res = web.post(
        "/tiers/new/",
        {"level": "1", "name": "ベーシック", "price_minor": "500"},
        HTTP_HOST=CREATOR_HOST,
    )
    assert res.status_code == 302
    assert CreatorTier.objects.filter(creator=c, level=1, name="ベーシック").exists()


@_HOSTS
def test_tier_create_level0_rejected(web, db):
    c = _creator()
    account = _account(c)
    _login_as(web, account)
    web.post(
        "/tiers/new/", {"level": "0", "name": "無料2", "price_minor": "0"}, HTTP_HOST=CREATOR_HOST
    )
    assert CreatorTier.objects.filter(creator=c, level=0).count() == 1


@_HOSTS
def test_tier_toggle_active(web, db):
    c = _creator()
    tier = CreatorTier.objects.create(creator=c, level=1, name="B", price_minor=500)
    account = _account(c)
    _login_as(web, account)
    web.post(f"/tiers/{tier.id}/toggle/", HTTP_HOST=CREATOR_HOST)
    tier.refresh_from_db()
    assert tier.is_active is False


@_HOSTS
def test_tier_toggle_other_creator_tier_404(web, db):
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    other_tier = CreatorTier.objects.create(creator=c2, level=1, name="B", price_minor=500)
    account = _account(c1)
    _login_as(web, account)
    res = web.post(f"/tiers/{other_tier.id}/toggle/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 404


# ---- series (閲覧) ----


@_HOSTS
def test_series_list_shows_linked_only(web, channel, db):
    c = _creator()
    _series(channel, creator=c, title="私の番組")
    account = _account(c)
    _login_as(web, account)
    res = web.get("/series/", HTTP_HOST=CREATOR_HOST)
    assert "私の番組" in res.content.decode("utf-8")


# ---- posts CRUD (テナント境界重点) ----


@_HOSTS
def test_posts_list_other_creator_series_404(web, channel, db):
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    other_series = _series(channel, creator=c2, title="他人の番組")
    account = _account(c1)
    _login_as(web, account)
    res = web.get(f"/series/{other_series.id}/posts/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 404


@_HOSTS
def test_post_create_with_free_level(web, channel, db):
    c = _creator()
    s = _series(channel, creator=c)
    account = _account(c)
    _login_as(web, account)
    res = web.post(
        f"/series/{s.id}/posts/new/",
        {"kind": "article", "title": "限定記事", "body": "本文", "fc_required_level": "0"},
        HTTP_HOST=CREATOR_HOST,
    )
    assert res.status_code == 302
    post = SeriesPost.objects.get(series=s, title="限定記事")
    assert post.fc_required_level == 0


@_HOSTS
def test_post_create_full_public(web, channel, db):
    c = _creator()
    s = _series(channel, creator=c)
    account = _account(c)
    _login_as(web, account)
    web.post(
        f"/series/{s.id}/posts/new/",
        {"kind": "article", "title": "通常記事", "body": "本文", "fc_required_level": ""},
        HTTP_HOST=CREATOR_HOST,
    )
    post = SeriesPost.objects.get(series=s, title="通常記事")
    assert post.fc_required_level is None


@_HOSTS
def test_post_create_with_invalid_paid_level_rejected(web, channel, db):
    """存在しない/無効なティアlevelを指定した場合はエラーで保存しない。"""
    c = _creator()
    s = _series(channel, creator=c)
    account = _account(c)
    _login_as(web, account)
    res = web.post(
        f"/series/{s.id}/posts/new/",
        {"kind": "article", "title": "不正記事", "body": "本文", "fc_required_level": "5"},
        HTTP_HOST=CREATOR_HOST,
        follow=True,
    )
    assert not SeriesPost.objects.filter(series=s, title="不正記事").exists()
    assert res.status_code == 200


@_HOSTS
def test_post_edit_other_creator_series_404(web, channel, db):
    c1 = _creator(slug="c1")
    c2 = _creator(slug="c2")
    other_series = _series(channel, creator=c2)
    post = SeriesPost.objects.create(series=other_series, title="他人の投稿", is_published=True)
    account = _account(c1)
    _login_as(web, account)
    res = web.get(f"/series/{other_series.id}/posts/{post.id}/edit/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 404


@_HOSTS
def test_post_delete(web, channel, db):
    c = _creator()
    s = _series(channel, creator=c)
    post = SeriesPost.objects.create(series=s, title="削除対象", is_published=True)
    account = _account(c)
    _login_as(web, account)
    res = web.post(f"/series/{s.id}/posts/{post.id}/delete/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302
    assert not SeriesPost.objects.filter(pk=post.id).exists()


@_HOSTS
def test_post_publish_sets_published_at(web, channel, db):
    c = _creator()
    s = _series(channel, creator=c)
    post = SeriesPost.objects.create(series=s, title="下書き", is_published=False)
    account = _account(c)
    _login_as(web, account)
    web.post(
        f"/series/{s.id}/posts/{post.id}/edit/",
        {
            "kind": "article",
            "title": "下書き",
            "body": "",
            "fc_required_level": "",
            "is_published": "on",
        },
        HTTP_HOST=CREATOR_HOST,
    )
    post.refresh_from_db()
    assert post.is_published is True
    assert post.published_at is not None


# ---- members summary (集計のみ) ----


@_HOSTS
def test_members_summary_shows_count_not_pii(web, channel, db):
    from members.models import Member

    c = _creator()
    tier = CreatorTier.objects.get(creator=c, level=0)
    m = Member(
        email="fan@example.com",
        nickname="fan",
        birth_year=1990,
        birth_month=1,
        postal_code="1000001",
    )
    m.set_password("Tv9!kd83mfar")  # pragma: allowlist secret - test only
    m.save()
    fc_services.join(m, c, tier)
    account = _account(c)
    _login_as(web, account)
    res = web.get("/members/", HTTP_HOST=CREATOR_HOST)
    body = res.content.decode("utf-8")
    assert "1 名" in body
    assert "fan@example.com" not in body  # PII は出さない


# ---- contracts (閲覧のみ) ----


@_HOSTS
def test_contracts_list_readonly(web, db):
    c = _creator()
    SlotContract.objects.create(
        creator=c,
        title="レギュラー枠",
        monthly_fee_minor=100000,
        starts_on="2026-08-01",
        status="active",
    )
    account = _account(c)
    _login_as(web, account)
    res = web.get("/contracts/", HTTP_HOST=CREATOR_HOST)
    assert "レギュラー枠" in res.content.decode("utf-8")


@_HOSTS
def test_contracts_list_shows_youtube_destination_label(web, db):
    c = _creator()
    SlotContract.objects.create(
        creator=c,
        title="レギュラー枠",
        monthly_fee_minor=100000,
        starts_on="2026-08-01",
        status="active",
        youtube_destination="creator_channel",
    )
    account = _account(c)
    _login_as(web, account)
    res = web.get("/contracts/", HTTP_HOST=CREATOR_HOST)
    assert "クリエイター自チャンネル" in res.content.decode("utf-8")


# ---- YouTube 宛先設定 (#27 Part B) ----


@_HOSTS
def test_youtube_destination_requires_login(web, db):
    res = web.get("/youtube/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url.startswith("/login/")


@_HOSTS
def test_youtube_destination_shows_unset_status(web, db):
    c = _creator()
    account = _account(c)
    _login_as(web, account)
    res = web.get("/youtube/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 200
    assert "未設定" in res.content.decode("utf-8")


@_HOSTS
def test_youtube_destination_save_sets_stream_key(web, db):
    c = _creator()
    account = _account(c)
    _login_as(web, account)
    res = web.post(
        "/youtube/",
        {"ingest_url": "rtmp://a.rtmp.youtube.com/live2", "stream_key": "my-secret-key"},
        HTTP_HOST=CREATOR_HOST,
    )
    assert res.status_code == 302
    c.refresh_from_db()
    assert c.youtube_destination_stream_key == "my-secret-key"
    assert c.youtube_destination_ingest_url == "rtmp://a.rtmp.youtube.com/live2"


@_HOSTS
def test_youtube_destination_save_without_key_keeps_existing(web, db):
    c = _creator()
    c.youtube_destination_stream_key = "existing-key"
    c.save(update_fields=["youtube_destination_stream_key"])
    account = _account(c)
    _login_as(web, account)
    web.post(
        "/youtube/",
        {"ingest_url": "rtmp://a.rtmp.youtube.com/live2", "stream_key": ""},
        HTTP_HOST=CREATOR_HOST,
    )
    c.refresh_from_db()
    assert c.youtube_destination_stream_key == "existing-key"


@_HOSTS
def test_youtube_destination_never_echoes_existing_key(web, db):
    """秘密値は画面へ再表示しない (パスワード入力欄も value 属性なし)。"""
    c = _creator()
    c.youtube_destination_stream_key = "super-secret-value"
    c.save(update_fields=["youtube_destination_stream_key"])
    account = _account(c)
    _login_as(web, account)
    res = web.get("/youtube/", HTTP_HOST=CREATOR_HOST)
    assert "super-secret-value" not in res.content.decode("utf-8")


@_HOSTS
def test_youtube_destination_requires_ingest_url(web, db):
    c = _creator()
    account = _account(c)
    _login_as(web, account)
    res = web.post(
        "/youtube/", {"ingest_url": "", "stream_key": "k"}, HTTP_HOST=CREATOR_HOST, follow=True
    )
    assert res.status_code == 200
    c.refresh_from_db()
    assert not c.youtube_destination_stream_key
