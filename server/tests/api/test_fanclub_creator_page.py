# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""クリエイター公開ファンクラブページ /fc/<slug>/ とポータルのページ設定 (#27 §10.2)。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorSeriesLink, CreatorTier
from members.models import Member
from scheduling.models import Program, ProgramType, Series, SeriesPost, SeriesPostKind

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com"):
    m = Member(email=email, nickname="t", birth_year=1990, birth_month=4, postal_code="1000001")
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a", **kw):
    c = Creator.objects.create(name="サークルA", slug=slug, **kw)
    fc_services.ensure_free_tier(c)
    return c


def _series(channel, creator, title="番組A"):
    s = Series.objects.create(channel=channel, title=title)
    CreatorSeriesLink.objects.create(series=s, creator=creator)
    return s


def _post(series, *, title="お知らせ", fc_required_level=None):
    return SeriesPost.objects.create(
        series=series,
        kind=SeriesPostKind.ARTICLE,
        title=title,
        body="本文",
        is_published=True,
        published_at=timezone.now(),
        fc_required_level=fc_required_level,
    )


# ---- 公開ページ ----


@_PUBLIC_HOST
def test_fc_page_renders_public_profile(http_client, channel, db):
    c = _creator()
    c.description = "よろしくお願いします"
    c.sns_x_url = "https://x.com/circle_a"
    c.save(update_fields=["description", "sns_x_url"])
    s = _series(channel, c)
    _post(s, title="公開のお知らせ")
    res = http_client.get(f"/fc/{c.slug}/")
    body = res.content.decode("utf-8")
    assert res.status_code == 200
    assert "サークルA" in body
    assert "よろしくお願いします" in body
    assert "https://x.com/circle_a" in body
    assert "番組A" in body
    assert "公開のお知らせ" in body


@_PUBLIC_HOST
def test_fc_page_404_for_suspended_creator(http_client, db):
    c = _creator(status="suspended")
    res = http_client.get(f"/fc/{c.slug}/")
    assert res.status_code == 404


@_PUBLIC_HOST
def test_fc_page_404_for_unknown_slug(http_client, db):
    res = http_client.get("/fc/no-such-creator/")
    assert res.status_code == 404


@_PUBLIC_HOST
def test_fc_page_locks_gated_posts_for_anonymous(http_client, channel, db):
    c = _creator()
    s = _series(channel, c)
    _post(s, title="限定記事", fc_required_level=0)
    res = http_client.get(f"/fc/{c.slug}/")
    body = res.content.decode("utf-8")
    assert "限定記事" in body  # タイトルはプレビューとして出す
    assert "🔒" in body


@_PUBLIC_HOST
def test_fc_page_unlocks_gated_posts_for_member(http_client, channel, db):
    c = _creator()
    s = _series(channel, c)
    _post(s, title="限定記事", fc_required_level=0)
    m = _member()
    fc_services.join_free_tier(m, c)
    _login(http_client)
    res = http_client.get(f"/fc/{c.slug}/")
    body = res.content.decode("utf-8")
    assert "限定記事" in body
    assert "🔒" not in body
    assert "デジタル会員証を表示" in body


@_PUBLIC_HOST
def test_fc_page_shows_upcoming_programs(http_client, channel, asset_ready, db):
    c = _creator()
    s = _series(channel, c)
    Program.objects.create(
        channel=channel,
        series=s,
        title="次回放送",
        type=ProgramType.RECORDED,
        asset=asset_ready,
        start_at=timezone.now() + timedelta(days=1),
        end_at=timezone.now() + timedelta(days=1, hours=1),
        public_visible=True,
    )
    res = http_client.get(f"/fc/{c.slug}/")
    assert "次回放送" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_fc_page_applies_valid_theme_color(http_client, db):
    c = _creator(theme_color="#ff8800")
    res = http_client.get(f"/fc/{c.slug}/")
    assert "--accent: #ff8800" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_fc_page_drops_invalid_theme_color(http_client, db):
    """バリデータを迂回して不正値が入っても描画側で落とす (CSSインジェクション防止)。"""
    c = _creator()
    # DB 列は varchar(7) なので、7文字以内で CSS を壊しうる値を直接入れる
    Creator.objects.filter(pk=c.pk).update(theme_color="};x:red")
    res = http_client.get(f"/fc/{c.slug}/")
    assert "--accent:" not in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_fc_page_join_button_for_anonymous(http_client, db):
    c = _creator()
    res = http_client.get(f"/fc/{c.slug}/")
    assert "ログインして参加" in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_fc_page_tier_cards_for_member(http_client, db):
    c = _creator(stripe_connect_account_id="acct_x", stripe_connect_onboarded=True)
    CreatorTier.objects.create(
        creator=c, level=1, name="ベーシック", price_minor=500, stripe_price_id="price_1"
    )
    _member()
    _login(http_client)
    res = http_client.get(f"/fc/{c.slug}/")
    body = res.content.decode("utf-8")
    assert "ベーシック" in body
    assert "/tiers/" in body  # 加入導線


@_PUBLIC_HOST
def test_series_detail_links_to_fc_page(http_client, channel, db):
    c = _creator()
    s = _series(channel, c)
    res = http_client.get(f"/series/{s.pk}/")
    assert f"/fc/{c.slug}/" in res.content.decode("utf-8")


# ---- クリエイターポータル: ページ設定 ----


def _creator_session(http_client, creator):
    """creator ポータルのログインセッションを直接作る (Google OAuth を迂回)。"""
    from fanclub.models import CreatorAccount

    account = CreatorAccount.objects.create(
        creator=creator, email="c@example.com", google_sub="sub-1"
    )
    session = http_client.session
    session["creator_account_id"] = account.pk
    session.save()
    return account


_CREATOR_HOST = override_settings(
    ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[], ICSTV_CREATOR_HOSTS=["testserver"]
)


@_CREATOR_HOST
def test_portal_profile_edit_saves_fields(http_client, db):
    c = _creator()
    _creator_session(http_client, c)
    res = http_client.post(
        "/profile/",
        {
            "description": "新しい紹介文",
            "avatar_url": "https://img.example.com/a.png",
            "cover_url": "https://img.example.com/c.png",
            "theme_color": "#22c55e",
            "sns_x_url": "https://x.com/abc",
            "sns_youtube_url": "",
            "sns_instagram_url": "",
            "website_url": "",
        },
        follow=True,
    )
    assert res.status_code == 200
    c.refresh_from_db()
    assert c.description == "新しい紹介文"
    assert c.theme_color == "#22c55e"
    assert c.avatar_url == "https://img.example.com/a.png"


@_CREATOR_HOST
def test_portal_profile_edit_rejects_bad_theme_color(http_client, db):
    c = _creator()
    _creator_session(http_client, c)
    res = http_client.post(
        "/profile/",
        {
            "description": "",
            "avatar_url": "",
            "cover_url": "",
            "theme_color": "red",  # hex 6桁以外は拒否
            "sns_x_url": "",
            "sns_youtube_url": "",
            "sns_instagram_url": "",
            "website_url": "",
        },
        follow=True,
    )
    msgs = [str(x) for x in res.context["messages"]]
    assert any("theme_color" in x for x in msgs)
    c.refresh_from_db()
    assert c.theme_color == ""


@_CREATOR_HOST
def test_portal_profile_edit_unaffected_by_other_fields(http_client, db):
    """別画面が入れた値の不備でこの保存が巻き添えで失敗しない (full_clean を編集列に限定)。"""
    c = _creator()
    # 特商法欄に不正なメールアドレスが既に入っている状況を作る (別画面/直投入の想定)
    Creator.objects.filter(pk=c.pk).update(contact_email="not-an-email")
    _creator_session(http_client, c)
    res = http_client.post(
        "/profile/",
        {
            "description": "保存できるべき",
            "avatar_url": "",
            "cover_url": "",
            "theme_color": "",
            "sns_x_url": "",
            "sns_youtube_url": "",
            "sns_instagram_url": "",
            "website_url": "",
        },
        follow=True,
    )
    msgs = [str(x) for x in res.context["messages"]]
    assert any("保存しました" in x for x in msgs)
    c.refresh_from_db()
    assert c.description == "保存できるべき"


@_CREATOR_HOST
def test_portal_profile_edit_requires_login(http_client, db):
    _creator()
    res = http_client.get("/profile/")
    assert res.status_code == 302 and "/login/" in res.url
