# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* ホスト: 認証(ログイン/OAuth/招待受諾)+ログアウト。

セルフサービス画面(dashboard/tiers/series/posts/members/contracts)は PR8 で追加する。
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from fanclub.creator_auth import (
    PENDING_INVITE_TOKEN_KEY,
    login_creator_account,
    logout_creator_account,
)
from fanclub.creator_decorators import creator_login_required
from fanclub.creator_oauth import CreatorOAuthNotConfiguredError, build_flow, verify_id_token
from fanclub.models import CreatorAccount, CreatorInvitation

_PENDING_NEXT_KEY = "creator_oauth_next"


def _safe_next(request, default="/") -> str:
    nxt = request.GET.get("next") or request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return default


def login_page(request):
    if getattr(request, "creator_account", None):
        return redirect("/")
    return render(request, "creator/login.html", {"next": _safe_next(request)})


@creator_login_required
@require_POST
def logout_view(request):
    logout_creator_account(request)
    return redirect("/login/")


def invite_landing(request, token: str):
    inv = CreatorInvitation.objects.filter(token=token).select_related("creator").first()
    if inv is None:
        return render(request, "creator/invite_landing.html", {"invalid": True}, status=404)
    expired = inv.expires_at < timezone.now()
    accepted = inv.accepted_at is not None
    return render(
        request,
        "creator/invite_landing.html",
        {"invitation": inv, "expired": expired, "accepted": accepted},
    )


def oauth_start(request):
    """/auth/google/start/?invite=<token> (invite省略=通常ログイン)。"""
    invite_token = request.GET.get("invite", "")
    if invite_token:
        inv = get_object_or_404(CreatorInvitation, token=invite_token)
        if inv.expires_at < timezone.now() or inv.accepted_at is not None:
            messages.error(request, "この招待は無効です(期限切れまたは受諾済み)。")
            return redirect("/login/")
    try:
        flow = build_flow(request)
    except CreatorOAuthNotConfiguredError as e:
        return render(request, "creator/login.html", {"oauth_error": str(e)}, status=503)
    auth_url, state = flow.authorization_url(access_type="online", prompt="select_account")
    request.session["creator_oauth_state"] = state
    request.session["creator_oauth_code_verifier"] = flow.code_verifier
    request.session[_PENDING_NEXT_KEY] = _safe_next(request)
    if invite_token:
        request.session[PENDING_INVITE_TOKEN_KEY] = invite_token
    else:
        request.session.pop(PENDING_INVITE_TOKEN_KEY, None)
    return redirect(auth_url)


def oauth_callback(request):
    expected_state = request.session.get("creator_oauth_state")
    got_state = request.GET.get("state")
    if not expected_state or expected_state != got_state:
        return render(
            request,
            "creator/login.html",
            {"oauth_error": "state不一致です。もう一度お試しください。"},
            status=400,
        )
    try:
        flow = build_flow(request, state=expected_state)
    except CreatorOAuthNotConfiguredError as e:
        return render(request, "creator/login.html", {"oauth_error": str(e)}, status=503)
    flow.code_verifier = request.session.get("creator_oauth_code_verifier")
    flow.fetch_token(authorization_response=request.build_absolute_uri())

    from django.conf import settings

    try:
        claims = verify_id_token(flow.credentials.id_token, settings.ICSTV_CREATOR_OAUTH_CLIENT_ID)
    except ValueError:
        return render(
            request,
            "creator/login.html",
            {"oauth_error": "Google認証の検証に失敗しました。"},
            status=400,
        )
    request.session.pop("creator_oauth_state", None)
    request.session.pop("creator_oauth_code_verifier", None)

    google_email = (claims.get("email") or "").strip().lower()
    google_sub = claims.get("sub") or ""
    if not claims.get("email_verified") or not google_email:
        return render(
            request,
            "creator/login.html",
            {"oauth_error": "メールアドレスが確認されていないGoogleアカウントです。"},
            status=400,
        )

    next_url = request.session.pop(_PENDING_NEXT_KEY, None) or "/"
    invite_token = request.session.pop(PENDING_INVITE_TOKEN_KEY, None)
    if invite_token:
        return _bind_via_invitation(request, invite_token, google_email, google_sub, next_url)
    return _login_existing_account(request, google_email, google_sub, next_url)


def _bind_via_invitation(
    request, invite_token: str, google_email: str, google_sub: str, next_url: str
):
    inv = CreatorInvitation.objects.filter(token=invite_token).select_related("creator").first()
    if inv is None or inv.expires_at < timezone.now() or inv.accepted_at is not None:
        messages.error(request, "この招待は無効です(期限切れまたは受諾済み)。")
        return redirect("/login/")
    if inv.email.strip().lower() != google_email:
        return render(
            request,
            "creator/login.html",
            {"oauth_error": "招待されたメールアドレスでログインしてください。"},
            status=400,
        )
    # get_or_create は email__iexact のような lookup を defaults と併用できないため明示的に分ける。
    account = CreatorAccount.objects.filter(creator=inv.creator, email__iexact=google_email).first()
    if account is None:
        account = CreatorAccount.objects.create(
            creator=inv.creator, email=google_email, google_sub=google_sub
        )
    elif not account.google_sub:
        account.google_sub = google_sub
        account.save(update_fields=["google_sub"])
    inv.accepted_at = timezone.now()
    inv.save(update_fields=["accepted_at"])
    login_creator_account(request, account)
    messages.success(request, f"{inv.creator.name} のクリエイターとして参加しました。")
    return redirect(next_url)


def _login_existing_account(request, google_email: str, google_sub: str, next_url: str):
    account = CreatorAccount.objects.filter(email__iexact=google_email, is_active=True).first()
    if account is None:
        return render(
            request,
            "creator/login.html",
            {
                "oauth_error": "このメールアドレスへの招待が見つかりません。運営からの招待URLをご利用ください。"
            },
            status=403,
        )
    if not account.google_sub:
        account.google_sub = google_sub
        account.save(update_fields=["google_sub"])
    login_creator_account(request, account)
    return redirect(next_url)
