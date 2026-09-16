# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""request.member を session / Bearer トークンから遅延復元する (AuthenticationMiddleware と同型)。

SessionMiddleware の後・AuthenticationMiddleware の直後に置く。ホスト非依存で動くが、
会員 URL は公開 urlconf (config.urls_public) にしか存在しないため、管理/納品ホストで
request.member が立っても無害。SimpleLazyObject なので request.member を参照しない
リクエスト (ライブ poster 配信や now JSON など) では DB を引かない。

session はブラウザ (React 島含む) 用、Bearer はネイティブアプリ用 (#MOBILE-01)。両方をここで
吸収することで、匿名でも会員でも動くエンドポイント (VOD 再生ゲートなど) が認証方式を意識せずに
済む。Bearer はブラウザが自動付与しないため CSRF の考慮は不要 (session 側の CSRF 強制は
api.auth.MemberAuth のまま変更していない)。
"""

from __future__ import annotations

from django.utils.functional import SimpleLazyObject

from members.auth import SESSION_KEY


def bearer_token(request) -> str | None:
    header = request.META.get("HTTP_AUTHORIZATION") or ""
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    return header[len(prefix) :].strip() or None


def _load_member(request):
    mid = request.session.get(SESSION_KEY)
    if mid:
        from members.models import Member

        return Member.objects.filter(pk=mid, is_active=True).first()

    # session が無いときだけトークンを見る。session を優先するのは、ブラウザからのリクエストに
    # 紛れ込んだ Authorization ヘッダでログイン中の会員が入れ替わらないようにするため。
    from members import api_tokens

    return api_tokens.resolve(bearer_token(request))


class MemberAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.member = SimpleLazyObject(lambda: _load_member(request))
        return self.get_response(request)
