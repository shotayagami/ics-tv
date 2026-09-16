# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員限定チャット (#27 §3.1 Should、Fanicon グルチャ型)。

投稿=HTTP / 配信=WS の分離 (#COMM-01 と同じ)。WS consumer のゲートは同期部分
(_can_join の中身 = services.can_use_chat) を直接検証する。
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from fanclub import services as fc_services
from fanclub.models import Creator, CreatorMembership, CreatorTier, FcChatMessage
from members.models import Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])
_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _member(email="m@example.com", *, verified=True):
    m = Member(
        email=email,
        nickname="たろう",
        birth_year=1990,
        birth_month=4,
        postal_code="1000001",
        email_verified_at=timezone.now() if verified else None,
    )
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _creator(slug="circle-a", *, chat_required_level=0):
    c = Creator.objects.create(name="サークルA", slug=slug, chat_required_level=chat_required_level)
    fc_services.ensure_free_tier(c)
    return c


# ---- 資格判定 (WS connect / SSR 共通の単一ソース) ----


def test_can_use_chat_requires_membership(db):
    c = _creator()
    m = _member()
    assert fc_services.can_use_chat(m, c) is False  # 未加入
    fc_services.join_free_tier(m, c)
    assert fc_services.can_use_chat(m, c) is True


def test_can_use_chat_disabled_when_level_null(db):
    c = _creator(chat_required_level=None)
    m = _member()
    fc_services.join_free_tier(m, c)
    assert fc_services.can_use_chat(m, c) is False


def test_can_use_chat_tier_gate(db):
    c = _creator(chat_required_level=1)
    t1 = CreatorTier.objects.create(creator=c, level=1, name="ベーシック", price_minor=500)
    m = _member()
    fc_services.join_free_tier(m, c)
    assert fc_services.can_use_chat(m, c) is False  # 無料会員では不足
    CreatorMembership.objects.filter(member=m, creator=c).update(tier=t1)
    assert fc_services.can_use_chat(m, c) is True


def test_can_use_chat_anonymous_and_none_creator(db):
    c = _creator()
    assert fc_services.can_use_chat(None, c) is False
    assert fc_services.can_use_chat(None, None) is False


# ---- 投稿 ----


@_PUBLIC_HOST
def test_chat_post_creates_message(http_client, db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/chat/post/", {"body": "こんにちは"})
    assert res.status_code == 302
    msg = FcChatMessage.objects.get(creator=c)
    assert msg.body == "こんにちは" and msg.member_id == m.pk


@_PUBLIC_HOST
def test_chat_post_403_for_non_member(http_client, db):
    c = _creator()
    _member()
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/chat/post/", {"body": "hi"})
    assert res.status_code == 403
    assert FcChatMessage.objects.count() == 0


@_PUBLIC_HOST
def test_chat_post_requires_verified_member(http_client, db):
    c = _creator()
    m = _member(verified=False)
    fc_services.join_free_tier(m, c)
    _login(http_client)
    res = http_client.post(f"/fanclub/{c.slug}/chat/post/", {"body": "hi"}, follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("本人確認" in x for x in msgs)
    assert FcChatMessage.objects.count() == 0


@_PUBLIC_HOST
def test_chat_post_cooldown(http_client, db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    _login(http_client)
    http_client.post(f"/fanclub/{c.slug}/chat/post/", {"body": "1"})
    res = http_client.post(f"/fanclub/{c.slug}/chat/post/", {"body": "2"}, follow=True)
    msgs = [str(x) for x in res.context["messages"]]
    assert any("間隔" in x for x in msgs)
    assert FcChatMessage.objects.count() == 1


@_PUBLIC_HOST
def test_chat_delete_own_only(http_client, db):
    c = _creator()
    author = _member("a@example.com")
    fc_services.join_free_tier(author, c)
    msg = FcChatMessage.objects.create(creator=c, member=author, body="消される")
    other = _member("b@example.com")
    fc_services.join_free_tier(other, c)
    _login(http_client, "b@example.com")
    res = http_client.post(f"/fanclub/{c.slug}/chat/{msg.pk}/delete/")
    assert res.status_code == 403
    http_client.post("/members/logout/")
    _login(http_client, "a@example.com")
    res = http_client.post(f"/fanclub/{c.slug}/chat/{msg.pk}/delete/")
    assert res.status_code == 302
    msg.refresh_from_db()
    assert msg.deleted_at is not None


# ---- ページ表示 ----


@_PUBLIC_HOST
def test_fc_page_shows_chat_for_member_with_badge(http_client, db):
    c = _creator()
    m = _member()
    membership = fc_services.join_free_tier(m, c)
    membership.joined_at = timezone.now() - timedelta(days=400)
    membership.save(update_fields=["joined_at"])
    FcChatMessage.objects.create(creator=c, member=m, body="やっほー")
    _login(http_client)
    res = http_client.get(f"/fc/{c.slug}/")
    body = res.content.decode("utf-8")
    assert "メンバーチャット" in body
    assert "やっほー" in body
    assert "継続1年" in body  # 勤続バッジがチャットに出る
    assert "/ws/fc/" in body  # WS 接続スクリプト


@_PUBLIC_HOST
def test_fc_page_hides_chat_body_from_non_member(http_client, db):
    c = _creator()
    poster = _member("a@example.com")
    fc_services.join_free_tier(poster, c)
    FcChatMessage.objects.create(creator=c, member=poster, body="ひみつの話")
    _member("b@example.com")
    _login(http_client, "b@example.com")
    res = http_client.get(f"/fc/{c.slug}/")
    body = res.content.decode("utf-8")
    assert "ひみつの話" not in body
    assert "参加できます" in body  # ゲート文言


@_PUBLIC_HOST
def test_fc_page_hides_chat_section_when_disabled(http_client, db):
    c = _creator(chat_required_level=None)
    m = _member()
    fc_services.join_free_tier(m, c)
    _login(http_client)
    res = http_client.get(f"/fc/{c.slug}/")
    # CSS コメントに機能名が残るため、セクション本体のアンカーで判定する
    assert 'id="fc-chat"' not in res.content.decode("utf-8")


@_PUBLIC_HOST
def test_fc_page_excludes_deleted_chat_messages(http_client, db):
    c = _creator()
    m = _member()
    fc_services.join_free_tier(m, c)
    FcChatMessage.objects.create(
        creator=c, member=m, body="消された発言", deleted_at=timezone.now()
    )
    _login(http_client)
    res = http_client.get(f"/fc/{c.slug}/")
    assert "消された発言" not in res.content.decode("utf-8")
