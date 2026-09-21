# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* の Google OAuth サインイン (#27)。

core/admin_views.py の YouTube OAuth (PKCE付き Flow) と同じ骨格を流用するが、目的は
「サインインのみ」(YouTube API アクセス権は不要) なので access_type="offline" ではなく
"online"、スコープは openid/email のみにする。id_token 検証は Node版 (icstv-delivery) の
tokeninfo HTTP 問い合わせと異なり、google-auth のローカル検証 (google.oauth2.id_token) を使う
(追加の外部HTTP往復が不要)。
"""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from google_auth_oauthlib.flow import Flow

_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


class CreatorOAuthNotConfiguredError(RuntimeError):
    pass


def build_flow(request, *, state: str | None = None) -> Flow:
    client_id = settings.ICSTV_CREATOR_OAUTH_CLIENT_ID
    client_secret = settings.ICSTV_CREATOR_OAUTH_CLIENT_SECRET
    if not (client_id and client_secret):
        raise CreatorOAuthNotConfiguredError("ICSTV_CREATOR_OAUTH_CLIENT_ID/SECRET が未設定")
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": _AUTH_URI,
                "token_uri": _TOKEN_URI,
            }
        },
        scopes=_SCOPES,
        state=state,
    )
    flow.redirect_uri = request.build_absolute_uri(reverse("creator_oauth_callback"))
    return flow


def verify_id_token(id_token_str: str, client_id: str) -> dict:
    """id_token をローカル検証し claims (sub/email/email_verified 等) を返す。不正なら例外送出。"""
    import google.auth.transport.requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        id_token_str, google.auth.transport.requests.Request(), audience=client_id
    )
