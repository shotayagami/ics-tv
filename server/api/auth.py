# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ninja 認証: 公開フロント会員 (members.Member) の session + CSRF、およびアプリ用 Bearer。

ブラウザ (React 島) は既存サイトと同一の session cookie を踏襲する。APIKeyCookie は既定
csrf=True で、非 GET に対し check_csrf(request) を実行する → React 島は csrftoken cookie を
X-CSRFToken で送る。key (session cookie 値) 自体は使わず、MemberAuthMiddleware が
session["member_id"] から付与した request.member を採用する。未ログインは None → 401。

ネイティブアプリ (#MOBILE-01) は cookie jar を前提にできないため Bearer トークンを使う。
両方を受けるエンドポイントでは member_any_auth を使う (並べる順序に意味がある。下記)。
"""

from django.conf import settings
from django.http import HttpRequest
from ninja.security import APIKeyCookie, HttpBearer, SessionAuthIsStaff

from members.auth import SESSION_KEY


class MemberAuth(APIKeyCookie):
    param_name = settings.SESSION_COOKIE_NAME

    def authenticate(self, request: HttpRequest, key: str | None):
        return getattr(request, "member", None)


class MemberTokenAuth(HttpBearer):
    """Authorization: Bearer <token> でアプリからログインする (#MOBILE-01)。

    トークンの照合自体は MemberAuthMiddleware が済ませている (session が無いときだけトークンを
    見る) ので、ここでは解決済みの request.member を採る。

    **session がある時は必ず None を返すこと**。ninja は最初に成功した auth callback で連鎖を
    打ち切るため、ここで session 由来の会員を返してしまうと、cookie で認証されている非 GET
    リクエストが Authorization ヘッダを添えるだけで MemberAuth の CSRF 検査を飛ばせてしまう
    (HttpBearer はスキーム名が bearer であることしか見ないので、トークンの中身は何でもよい)。
    session 由来の識別は CSRF を担う MemberAuth に処理させる。
    """

    def authenticate(self, request: HttpRequest, token: str):
        if request.session.get(SESSION_KEY):
            return None
        return getattr(request, "member", None)


member_auth = MemberAuth()
member_token_auth = MemberTokenAuth()

# session と Bearer の両方を受けるエンドポイント用。
#
# **順序を入れ替えないこと**: ninja の _run_authentication は callback が例外を投げた時点で
# 連鎖を打ち切る (次の callback へ進まない)。MemberAuth は CSRF 不一致で HttpError(403) を
# 投げるため、これを先に置くと cookie を持たない Bearer リクエストがトークンを試される前に
# 403 で落ちる。トークンを先に置けば、Bearer 有り→即成功 / Bearer 無し→None を返して
# cookie 版へ進む、とどちらの経路も正しく通る。
member_any_auth = [member_token_auth, member_auth]

# studio 管理 SPA (#Phase2d) 用。Django 標準の session (request.user) で staff/superuser を判定。
# SessionAuthIsStaff は APIKeyCookie 派生なので非 GET は CSRF を強制する (公開会員 API と同じ作法)。
# 公開会員 (members.Member) は request.user=AnonymousUser なので弾かれる → 管理 API は staff 限定。
staff_auth = SessionAuthIsStaff()
