# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* Google招待サインイン (#27)。OAuthのGoogle往復はモックし、bind/loginロジックを検証する。"""

from __future__ import annotations

import secrets
from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from fanclub.models import Creator, CreatorAccount, CreatorInvitation

CREATOR_HOST = "creator.example.com"
_HOSTS = override_settings(
    ALLOWED_HOSTS=["testserver", CREATOR_HOST],
    ICSTV_ADMIN_HOSTS=[],
    ICSTV_CREATOR_HOSTS=[CREATOR_HOST],
    ICSTV_CREATOR_OAUTH_CLIENT_ID="test-client-id",
    ICSTV_CREATOR_OAUTH_CLIENT_SECRET="test-client-secret",  # pragma: allowlist secret - test only
)


@pytest.fixture
def web():
    return Client()


def _creator(slug="circle-a"):
    return Creator.objects.create(name="サークルA", slug=slug)


def _invitation(creator, email="creator@example.com", expires_in_days=7, accepted=False):
    return CreatorInvitation.objects.create(
        creator=creator,
        email=email,
        token=secrets.token_urlsafe(16),
        expires_at=timezone.now() + timedelta(days=expires_in_days),
        accepted_at=timezone.now() if accepted else None,
    )


class _FakeCredentials:
    def __init__(self, id_token="fake-id-token"):
        self.id_token = id_token


class _FakeFlow:
    """google_auth_oauthlib.flow.Flow の最小モック。authorization_url/fetch_token/code_verifier のみ。"""

    def __init__(self):
        self.code_verifier = "verifier-xyz"
        self.credentials = _FakeCredentials()

    def authorization_url(self, **kwargs):
        return "https://accounts.google.com/o/oauth2/auth?mock=1", "state-abc123"

    def fetch_token(self, **kwargs):
        pass


def _start_oauth(web, invite_token=None):
    url = "/auth/google/start/"
    if invite_token:
        url += f"?invite={invite_token}"
    with mock.patch("fanclub.creator_views.build_flow", return_value=_FakeFlow()):
        res = web.get(url, HTTP_HOST=CREATOR_HOST)
    return res


def _callback(web, claims):
    with (
        mock.patch("fanclub.creator_views.build_flow", return_value=_FakeFlow()),
        mock.patch("fanclub.creator_views.verify_id_token", return_value=claims),
    ):
        res = web.get("/auth/google/callback/?state=state-abc123", HTTP_HOST=CREATOR_HOST)
    return res


def _claims(email="creator@example.com", sub="google-sub-1", verified=True):
    return {"email": email, "sub": sub, "email_verified": verified}


# ---- 招待受諾 ----


@_HOSTS
def test_invite_landing_shows_invitation(web, db):
    c = _creator()
    inv = _invitation(c)
    res = web.get(f"/invite/{inv.token}/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 200
    assert c.name in res.content.decode("utf-8")


@_HOSTS
def test_invite_landing_unknown_token_404(web, db):
    res = web.get("/invite/no-such-token/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 404


@_HOSTS
def test_invite_landing_expired(web, db):
    c = _creator()
    inv = _invitation(c, expires_in_days=-1)
    res = web.get(f"/invite/{inv.token}/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 200
    assert "期限が切れています" in res.content.decode("utf-8")


@_HOSTS
def test_invite_landing_already_accepted(web, db):
    c = _creator()
    inv = _invitation(c, accepted=True)
    res = web.get(f"/invite/{inv.token}/", HTTP_HOST=CREATOR_HOST)
    assert "既に使用済み" in res.content.decode("utf-8")


@_HOSTS
def test_invite_accept_creates_account_and_logs_in(web, db):
    c = _creator()
    inv = _invitation(c, email="creator@example.com")
    _start_oauth(web, invite_token=inv.token)
    res = _callback(web, _claims(email="creator@example.com"))
    assert res.status_code == 302 and res.url == "/"

    account = CreatorAccount.objects.get(creator=c, email="creator@example.com")
    assert account.google_sub == "google-sub-1"
    inv.refresh_from_db()
    assert inv.accepted_at is not None

    # ログイン確立の確認 (以後 creator_login_required なページに到達できる)
    res2 = web.post("/logout/", HTTP_HOST=CREATOR_HOST)
    assert res2.status_code == 302  # 401/403でなくログイン済みとして処理された


@_HOSTS
def test_invite_accept_expired_rejected(web, db):
    """期限切れ招待は oauth_start の時点で早期リダイレクトされる (Googleへ往復させない)。"""
    c = _creator()
    inv = _invitation(c, email="creator@example.com", expires_in_days=-1)
    res = _start_oauth(web, invite_token=inv.token)
    assert res.status_code == 302 and res.url == "/login/"
    assert not CreatorAccount.objects.filter(creator=c).exists()


@_HOSTS
def test_invite_accept_email_mismatch_rejected(web, db):
    c = _creator()
    inv = _invitation(c, email="creator@example.com")
    _start_oauth(web, invite_token=inv.token)
    res = _callback(web, _claims(email="someone-else@example.com"))
    assert res.status_code == 400
    assert "招待されたメールアドレス" in res.content.decode("utf-8")
    assert not CreatorAccount.objects.filter(creator=c).exists()


@_HOSTS
def test_invite_accept_unverified_email_rejected(web, db):
    c = _creator()
    inv = _invitation(c, email="creator@example.com")
    _start_oauth(web, invite_token=inv.token)
    res = _callback(web, _claims(email="creator@example.com", verified=False))
    assert res.status_code == 400
    assert not CreatorAccount.objects.filter(creator=c).exists()


# ---- state不一致 ----


@_HOSTS
def test_oauth_state_mismatch_400(web, db):
    with mock.patch("fanclub.creator_views.build_flow", return_value=_FakeFlow()):
        res = web.get("/auth/google/callback/?state=wrong-state", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 400


# ---- 2回目以降ログイン (招待トークン無し) ----


@_HOSTS
def test_second_login_without_invite_succeeds(web, db):
    c = _creator()
    CreatorAccount.objects.create(creator=c, email="creator@example.com", google_sub="google-sub-1")
    _start_oauth(web)  # invite無し
    res = _callback(web, _claims(email="creator@example.com"))
    assert res.status_code == 302 and res.url == "/"


@_HOSTS
def test_login_without_invitation_rejected(web, db):
    _start_oauth(web)
    res = _callback(web, _claims(email="nobody@example.com"))
    assert res.status_code == 403
    assert not CreatorAccount.objects.filter(email="nobody@example.com").exists()


# ---- creator_login_required ----


@_HOSTS
def test_creator_login_required_redirects_anonymous(web, db):
    res = web.post("/logout/", HTTP_HOST=CREATOR_HOST)
    assert res.status_code == 302 and res.url.startswith("/login/")
