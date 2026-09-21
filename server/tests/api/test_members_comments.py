# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理: チャンネルコメント (確認済み会員のみ投稿・全員閲覧・本人 soft-delete)。"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from members.models import Comment, Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only


def _make_member(email="m@example.com", verified=True):
    m = Member(
        email=email, nickname="みんと", birth_year=1990, birth_month=4, postal_code="1000001"
    )
    if verified:
        m.email_verified_at = timezone.now()
    m.set_password(_PW)
    m.save()
    return m


def _login(http_client, email="m@example.com"):
    http_client.post("/members/login/", {"email": email, "password": _PW})


def _current_program(channel, asset, title="放送中の番組"):
    from scheduling.models import Program, ProgramType

    now = timezone.now()
    return Program.objects.create(
        channel=channel,
        type=ProgramType.RECORDED,
        title=title,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=50),
        asset=asset,
        public_visible=True,
    )


@_PUBLIC_HOST
def test_verified_member_can_post(http_client, channel, db):
    m = _make_member()
    _login(http_client)
    res = http_client.post("/members/comments/ch1/post/", {"body": "こんにちは"})
    assert res.status_code == 302 and res.url == "/ch/ch1/#comments"
    c = Comment.objects.get()
    assert c.member_id == m.pk and c.body == "こんにちは" and c.channel_id == channel.id
    assert c.program_title == ""  # 放送中番組なし → 空スナップショット


@_PUBLIC_HOST
def test_post_snapshots_current_program_title(http_client, channel, asset_ready, db):
    _make_member()
    _login(http_client)
    _current_program(channel, asset_ready, title="ニュース")
    http_client.post("/members/comments/ch1/post/", {"body": "面白い"})
    assert Comment.objects.get().program_title == "ニュース"


@_PUBLIC_HOST
def test_unverified_member_gated_to_verify(http_client, channel, db):
    _make_member(verified=False)
    _login(http_client)
    res = http_client.post("/members/comments/ch1/post/", {"body": "x"})
    assert res.status_code == 302 and res.url == "/members/verify-required/"
    assert not Comment.objects.exists()


@_PUBLIC_HOST
def test_anon_redirected_to_login(http_client, channel, db):
    res = http_client.post("/members/comments/ch1/post/", {"body": "x"})
    assert res.status_code == 302 and res.url.startswith("/members/login/")
    assert not Comment.objects.exists()


@_PUBLIC_HOST
def test_anyone_can_read_comments_via_api(http_client, channel, db):
    # #Phase1: コメントは React 島が ninja API から描画する。未ログインでも読める。
    m = _make_member()
    Comment.objects.create(channel=channel, member=m, body="表示テスト")
    data = http_client.get("/api/v1/channels/ch1/comments").json()
    assert "表示テスト" in [c["body"] for c in data["items"]]
    assert "みんと" in [c["nickname"] for c in data["items"]]


@_PUBLIC_HOST
def test_delete_own_comment(http_client, channel, db):
    m = _make_member()
    c = Comment.objects.create(channel=channel, member=m, body="消す")
    _login(http_client)
    res = http_client.post(f"/members/comments/{c.id}/delete/")
    assert res.status_code == 302
    c.refresh_from_db()
    assert c.deleted_at is not None
    # 非表示後は API 一覧に出ない (#Phase1: 島が API から描画)
    items = http_client.get("/api/v1/channels/ch1/comments").json()["items"]
    assert all(c["body"] != "消す" for c in items)


@_PUBLIC_HOST
def test_cannot_delete_others_comment(http_client, channel, db):
    m1 = _make_member("a@example.com")
    _make_member("b@example.com")
    c = Comment.objects.create(channel=channel, member=m1, body="他人の")
    _login(http_client, "b@example.com")
    res = http_client.post(f"/members/comments/{c.id}/delete/")
    assert res.status_code == 403
    c.refresh_from_db()
    assert c.deleted_at is None


@_PUBLIC_HOST
def test_cooldown_blocks_rapid_post(http_client, channel, db):
    _make_member()
    _login(http_client)
    http_client.post("/members/comments/ch1/post/", {"body": "one"})
    http_client.post("/members/comments/ch1/post/", {"body": "two"})  # クールダウン中
    assert Comment.objects.count() == 1


def _give_comment_perk(member):
    from subscriptions.models import MemberSubscription, Plan, SubStatus

    plan = Plan.objects.create(name="松", slug="matsu", amount=980, rank=3, feat_comment_perk=True)
    MemberSubscription.objects.create(
        member=member,
        plan=plan,
        status=SubStatus.ACTIVE,
        current_period_end=timezone.now() + timedelta(days=30),
    )


@_PUBLIC_HOST
def test_plain_member_cooldown_blocks_after_3s(http_client, channel, db):
    # 特典なし会員は 5s クールダウン: 3 秒経過でもまだ弾かれる。
    m = _make_member()
    _login(http_client)
    http_client.post("/members/comments/ch1/post/", {"body": "one"})
    Comment.objects.filter(member=m).update(created_at=timezone.now() - timedelta(seconds=3))
    http_client.post("/members/comments/ch1/post/", {"body": "two"})
    assert Comment.objects.count() == 1


@_PUBLIC_HOST
def test_comment_perk_relaxes_cooldown(http_client, channel, db):
    # comment_perk 保持者はクールダウンが短い (5s→2s): 3 秒経過なら通る。
    m = _make_member()
    _give_comment_perk(m)
    _login(http_client)
    http_client.post("/members/comments/ch1/post/", {"body": "one"})
    Comment.objects.filter(member=m).update(created_at=timezone.now() - timedelta(seconds=3))
    http_client.post("/members/comments/ch1/post/", {"body": "two"})
    assert Comment.objects.count() == 2


_AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@_PUBLIC_HOST
def test_ajax_post_returns_partial_not_redirect(http_client, channel, db):
    # fetch 投稿はリロードさせない: redirect でなく #comments パーシャル(200)を返す。
    _make_member()
    _login(http_client)
    res = http_client.post("/members/comments/ch1/post/", {"body": "やあ"}, **_AJAX)
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert 'id="comments"' in body and "やあ" in body
    assert Comment.objects.count() == 1


@_PUBLIC_HOST
def test_ajax_cooldown_returns_partial_with_error(http_client, channel, db):
    # クールダウンでもリロードせず、エラー文言入りパーシャルを返す。
    _make_member()
    _login(http_client)
    http_client.post("/members/comments/ch1/post/", {"body": "one"}, **_AJAX)
    res = http_client.post("/members/comments/ch1/post/", {"body": "two"}, **_AJAX)
    assert res.status_code == 200
    assert "投稿が早すぎます" in res.content.decode("utf-8")
    assert Comment.objects.count() == 1


@_PUBLIC_HOST
def test_ajax_delete_returns_partial(http_client, channel, db):
    m = _make_member()
    c = Comment.objects.create(channel=channel, member=m, body="消す")
    _login(http_client)
    res = http_client.post(f"/members/comments/{c.id}/delete/", **_AJAX)
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert 'id="comments"' in body and "消す" not in body
    c.refresh_from_db()
    assert c.deleted_at is not None


@_PUBLIC_HOST
def test_api_returns_comment_body_as_raw_json_string(http_client, channel, db):
    # #Phase1: API は body を生テキストの JSON 文字列で返す (サーバで HTML を組まない)。
    # 描画時のエスケープは React が行う (XSS 面はサーバ HTML を持たないことで縮小)。
    _make_member()
    _login(http_client)
    http_client.post("/members/comments/ch1/post/", {"body": "<script>alert(1)</script>"})
    data = http_client.get("/api/v1/channels/ch1/comments").json()
    assert data["items"][0]["body"] == "<script>alert(1)</script>"
