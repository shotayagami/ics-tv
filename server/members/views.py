# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員ビュー (公開ホスト config.urls_public 配下 /members/)。

登録/ログイン/ログアウト/プロフィール/パスワード変更。メール検証 (commit②) と
TOTP 2FA (commit③) は別モジュール/別ビューで追加する。
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core.security_log import emit as security_emit
from members import api_tokens, codes, throttle, totp
from members.auth import (
    get_pending_member,
    get_pending_method,
    login_member,
    logout_member,
    start_pending_2fa,
)
from members.decorators import member_login_required, member_verified_required
from members.forms import (
    MemberAccountDeleteForm,
    MemberEmailChangeForm,
    MemberLoginForm,
    MemberPasswordChangeForm,
    MemberProfileForm,
    MemberRegistrationForm,
    PasswordResetConfirmForm,
    PasswordResetRequestForm,
    normalize_email,
)
from members.models import (
    ChannelFavorite,
    CodePurpose,
    Comment,
    Favorite,
    Member,
    Reminder,
    TwoFactorMethod,
    WatchHistory,
)

logger = logging.getLogger(__name__)

_LOGIN_NEXT_KEY = "member_login_next"
_TOTP_SETUP_KEY = "member_totp_setup_secret"
_COMMENT_COOLDOWN = 5  # 連投クールダウン秒
_COMMENT_COOLDOWN_PERK = 2  # comment_perk 特典: 連投クールダウン緩和
_COMMENT_MAX_LEN = 500


def _safe_next(request, default="/members/") -> str:
    """オープンリダイレクト防止: 同一サイト内のみ許可 (M-2)。

    自前の startswith 判定はバックスラッシュ回避 (例 /\\evil.com → ブラウザが //evil.com に
    正規化) を見落とすため、Django 標準の url_has_allowed_host_and_scheme に統一する。
    """
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return default


@require_http_methods(["GET", "POST"])
def register(request):
    if getattr(request, "member", None):
        return redirect("/members/")
    if request.method == "POST":
        form = MemberRegistrationForm(request.POST)
        if form.is_valid():
            member = form.create_member()
            login_member(request, member)
            try:
                codes.send_code(member, CodePurpose.EMAIL_VERIFY)
                messages.success(
                    request, "会員登録が完了しました。確認コードをメールに送信しました。"
                )
            except Exception:
                # メール基盤未整備でも登録は成立させる (仕様: 受け取れない人は未認証のまま使える)
                logger.exception("registration email code send failed: member=%s", member.pk)
                messages.success(request, "会員登録が完了しました。")
                messages.error(request, "確認コードの送信に失敗しました。あとで再送できます。")
            return redirect("/members/verify/email/")
    else:
        form = MemberRegistrationForm()
    return render(request, "members/register.html", {"form": form})


@require_http_methods(["GET", "POST"])
def login_view(request):
    if getattr(request, "member", None):
        return redirect("/members/")
    form = MemberLoginForm(request.POST or None)
    if request.method == "POST":
        ip = throttle.client_ip(request)
        # IP ロックは email 取得前 (form 検証前) に判定できる。Cookie/セッション非依存。
        if throttle.is_locked(throttle.LOGIN_IP, ip):
            security_emit(
                "auth.member.login", outcome="blocked", request=request, reason="ip_locked"
            )
            messages.error(request, "試行回数が多すぎます。しばらくしてからお試しください。")
        elif form.is_valid():
            email = normalize_email(form.cleaned_data["email"])
            password = form.cleaned_data["password"]
            email_key = throttle.hash_email(email)
            if throttle.is_locked(throttle.LOGIN, email_key):
                security_emit(
                    "auth.member.login",
                    outcome="blocked",
                    request=request,
                    reason="email_locked",
                    email_hash=email_key,
                )
                messages.error(request, "試行回数が多すぎます。しばらくしてからお試しください。")
            else:
                member = Member.objects.filter(email__iexact=email, is_active=True).first()
                if member is not None and member.check_password(password):
                    # 成功: email 側カウンタは消す。IP 側は同一 IP の攻撃者を守らないため残す
                    # (上限が高いので正規ユーザの誤入力では到達しない)。
                    throttle.clear(throttle.LOGIN, email_key)
                    security_emit(
                        "auth.member.login",
                        outcome="success",
                        request=request,
                        member_id=member.pk,
                        email_hash=email_key,
                    )
                    return _complete_or_challenge(request, member)
                if member is None:
                    # 存在しなくてもハッシュ計算してタイミング差/列挙を平す (Django auth と同手法)
                    Member().set_password(password)
                throttle.record_failure(throttle.LOGIN, email_key)
                throttle.record_failure(throttle.LOGIN_IP, ip)
                security_emit(
                    "auth.member.login",
                    outcome="fail",
                    request=request,
                    email_hash=email_key,
                )
                messages.error(request, "メールアドレスまたはパスワードが違います。")
    return render(request, "members/login.html", {"form": form, "next": _safe_next(request)})


@require_POST
def logout_view(request):
    logout_member(request)
    return redirect("/")


@member_login_required
def profile(request):
    return render(request, "members/profile.html", {})


@member_login_required
@require_http_methods(["GET", "POST"])
def profile_edit(request):
    form = MemberProfileForm(request.POST or None, instance=request.member)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "プロフィールを更新しました。")
        return redirect("/members/")
    return render(request, "members/profile_edit.html", {"form": form})


@member_login_required
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = MemberPasswordChangeForm(request.member, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        request.session.cycle_key()  # パスワード変更後もセッションを維持しつつ鍵を更新
        # 「パスワードを変えたら他の端末からは追い出される」を成立させる (#MOBILE-01)。
        # session 側は cycle_key で無効化されるが、アプリのトークンは別レイヤなので明示的に失効させる。
        api_tokens.revoke_all(request.member)
        messages.success(request, "パスワードを変更しました。他の端末のログインは解除されました。")
        return redirect("/members/")
    return render(request, "members/password_change.html", {"form": form})


# --- パスワードリセット (未ログイン。メールコードで再設定) ---
@require_http_methods(["GET", "POST"])
def password_reset_request(request):
    if getattr(request, "member", None):
        return redirect("/members/")
    form = PasswordResetRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        member = Member.objects.filter(email__iexact=email, is_active=True).first()
        if member is not None:
            try:
                codes.send_code(member, CodePurpose.PASSWORD_RESET)
            except codes.SendThrottledError:
                pass  # 直近に送信済 → 既存コードで継続
            except Exception:
                logger.exception("password reset send failed: member=%s", member.pk)
        # 列挙対策: 会員の存在有無にかかわらず同一応答
        messages.success(request, "登録済みのメールアドレスであれば、再設定コードを送信しました。")
        return redirect(f"/members/password/reset/confirm/?email={email}")
    return render(request, "members/password_reset_request.html", {"form": form})


@require_http_methods(["GET", "POST"])
def password_reset_confirm(request):
    if getattr(request, "member", None):
        return redirect("/members/")
    form = PasswordResetConfirmForm(
        request.POST or None, initial={"email": request.GET.get("email", "")}
    )
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        member = Member.objects.filter(email__iexact=email, is_active=True).first()
        if member is not None and codes.verify_code(
            member, CodePurpose.PASSWORD_RESET, form.cleaned_data["code"]
        ):
            member.set_password(form.cleaned_data["new_password"])
            member.save(update_fields=["password", "updated_at"])
            # リセットは「乗っ取られたので締め出したい」経路でもある。旧パスワードを知らなくても
            # 到達できる以上、既存のアプリトークンを残すと締め出しにならない (#MOBILE-01)。
            api_tokens.revoke_all(member)
            messages.success(
                request, "パスワードを再設定しました。新しいパスワードでログインしてください。"
            )
            return redirect("/members/login/")
        messages.error(request, "コードが正しくないか、有効期限が切れています。")
    return render(request, "members/password_reset_confirm.html", {"form": form})


# --- 退会 (アカウント削除。パスワード再入力 + PII ハード削除) ---
@member_login_required
@require_http_methods(["GET", "POST"])
def account_delete(request):
    form = MemberAccountDeleteForm(request.member, request.POST or None)
    if request.method == "POST" and form.is_valid():
        # 会員行を削除 (MemberEmailCode は CASCADE で消える) → セッション破棄
        Member.objects.filter(pk=request.member.pk).delete()
        logout_member(request)
        messages.success(request, "退会が完了しました。ご利用ありがとうございました。")
        return redirect("/")
    return render(request, "members/account_delete.html", {"form": form})


# --- メールアドレス変更 (新アドレスへコード送信 → 確認で確定・再検証) ---
@member_login_required
@require_http_methods(["GET", "POST"])
def email_change(request):
    member = request.member
    form = MemberEmailChangeForm(member, request.POST or None)
    if request.method == "POST" and form.is_valid():
        new_email = form.cleaned_data["new_email"]
        Member.objects.filter(pk=member.pk).update(pending_email=new_email)
        member.pending_email = new_email  # send_code 後の verify は member PK で引くので影響なし
        try:
            codes.send_code(member, CodePurpose.EMAIL_CHANGE, to=new_email)
            messages.success(request, f"{new_email} に確認コードを送信しました。")
        except codes.SendThrottledError as e:
            messages.error(request, f"{e.retry_after}秒後に再度お試しください。")
        except Exception:
            logger.exception("email change send failed: member=%s", member.pk)
            messages.error(request, "メール送信に失敗しました。時間をおいて再度お試しください。")
        return redirect("/members/email/confirm/")
    return render(request, "members/email_change.html", {"form": form})


@member_login_required
@require_http_methods(["GET", "POST"])
def email_change_confirm(request):
    member = request.member
    pending = member.pending_email
    if not pending:
        return redirect("/members/email/")
    if request.method == "POST":
        if codes.verify_code(member, CodePurpose.EMAIL_CHANGE, request.POST.get("code", "")):
            # 確定直前に再度衝突チェック (発行〜確認の間に他者が取得した場合)
            if Member.objects.filter(email__iexact=pending).exclude(pk=member.pk).exists():
                Member.objects.filter(pk=member.pk).update(pending_email=None)
                messages.error(
                    request, "このメールアドレスは既に使われています。最初からやり直してください。"
                )
                return redirect("/members/email/")
            Member.objects.filter(pk=member.pk).update(
                email=pending, pending_email=None, email_verified_at=timezone.now()
            )
            messages.success(request, "メールアドレスを変更しました。")
            return redirect("/members/")
        messages.error(request, "コードが正しくないか、有効期限が切れています。")
    return render(request, "members/email_change_confirm.html", {"pending_email": pending})


# --- チャンネルコメント (ライブチャット風。確認済み会員のみ投稿・全員閲覧) ---
def _current_program_title(channel) -> str:
    """投稿時の放送中番組名スナップショット (無ければ空)。"""
    from scheduling.models import Program

    now = timezone.now()
    cur = (
        Program.objects.filter(
            channel=channel, start_at__lte=now, end_at__gt=now, public_visible=True
        )
        .order_by("-start_at")
        .first()
    )
    return cur.title if cur else ""


def _is_ajax(request) -> bool:
    """fetch からの投稿/削除か (ページ無リロードで #comments だけ差し替えるための判定)。"""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _render_comments(request, channel):
    """コメント欄パーシャル (#comments) を最新状態で再描画。

    AJAX 投稿/削除の応答に使う。クライアントはこの HTML で #comments を差し替えるだけで、
    HLS プレイヤーには触れない (= 再生が止まらない)。messages も同梱されエラー文言を反映する。
    """
    from subscriptions.services import comment_perk_member_ids

    comments = list(
        Comment.objects.filter(channel=channel, deleted_at__isnull=True)
        .select_related("member")
        .order_by("-created_at")[:50]
    )
    # 公開ページと同じく comment_perk 保持者にバッジ (AJAX 再描画でバッジが消えないように供給)
    comment_badge_ids = comment_perk_member_ids({c.member_id for c in comments})
    return render(
        request,
        "public/_comments.html",
        {"channel": channel, "comments": comments, "comment_badge_ids": comment_badge_ids},
    )


@member_verified_required
@require_POST
def comment_post(request, slug):
    from core.models import Channel
    from subscriptions.models import ENT_COMMENT_PERK
    from subscriptions.services import entitlements

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    body = (request.POST.get("body") or "").strip()
    # コメント特典: 連投クールダウンを緩和 (特典フラグを実効化)
    ents = entitlements(request.member)
    has_perk = ENT_COMMENT_PERK in ents
    cooldown = _COMMENT_COOLDOWN_PERK if has_perk else _COMMENT_COOLDOWN
    last = Comment.objects.filter(member=request.member).order_by("-created_at").first()
    if not body:
        messages.error(request, "コメントを入力してください。")
    elif last and (timezone.now() - last.created_at).total_seconds() < cooldown:
        messages.error(request, "投稿が早すぎます。少し待ってからお試しください。")
    else:
        comment = Comment.objects.create(
            channel=channel,
            member=request.member,
            body=body[:_COMMENT_MAX_LEN],
            program_title=_current_program_title(channel),
        )
        _broadcast_new_comment(channel, comment, badge=has_perk)  # #COMM-01 リアルタイム配信
    if _is_ajax(request):
        return _render_comments(request, channel)
    return redirect(f"/ch/{slug}/#comments")


def _broadcast_new_comment(channel, comment, *, badge: bool) -> None:
    """新規コメントを同 channel の WS group へ配信 (#COMM-01)。

    投稿の成否に影響させない best-effort (channel layer 未設定/障害でも投稿は成功させる)。
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from members.consumers import comment_group
    from members.serializers import comment_payload

    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            comment_group(channel.slug),
            {"type": "comment.new", "comment": comment_payload(comment, badge=badge)},
        )
    except Exception:
        logger.warning("comment broadcast failed", exc_info=True)


@member_login_required
@require_POST
def comment_delete(request, comment_id):
    comment = get_object_or_404(Comment, pk=comment_id, deleted_at__isnull=True)
    if comment.member_id != request.member.pk:
        return HttpResponseForbidden("自分のコメントのみ削除できます。")
    channel = comment.channel
    Comment.objects.filter(pk=comment.pk).update(deleted_at=timezone.now())
    if _is_ajax(request):
        return _render_comments(request, channel)
    return redirect(f"/ch/{channel.slug}/#comments")


# --- マイリスト (あとで見る) #PERS-01 ---
@member_login_required
@require_POST
def favorite_toggle(request, program_id):
    from scheduling.models import Program

    program = get_object_or_404(Program, pk=program_id, public_visible=True)
    fav = Favorite.objects.filter(member=request.member, program=program).first()
    if fav:
        fav.delete()
        favorited = False
    else:
        Favorite.objects.create(member=request.member, program=program)
        favorited = True
    if _is_ajax(request):
        return JsonResponse({"favorited": favorited})
    return redirect(f"/program/{program_id}/")


@member_login_required
def favorites_list(request):
    favs = (
        Favorite.objects.filter(member=request.member)
        .select_related("program", "program__channel", "program__series")
        .order_by("-created_at")
    )
    return render(request, "members/favorites.html", {"programs": [f.program for f in favs]})


# --- ファンクラブ (デジタル会員証) #27 §3.1 Must ---
def _my_active_memberships(member):
    """在籍中のファンクラブ。未採番の会員番号はここで遅延採番する。

    採番点は加入時 (fanclub.services.join / webhook) だが、この機能の実装前から在籍している行は
    未採番のまま残っているため、表示のたびに取りこぼしを埋める。
    """
    from fanclub import services as fc_services
    from fanclub.models import CreatorMembership, MembershipStatus

    memberships = list(
        CreatorMembership.objects.filter(member=member, status=MembershipStatus.ACTIVE)
        .select_related("creator", "tier", "pending_tier")
        .order_by("joined_at", "id")
    )
    for m in memberships:
        fc_services.ensure_member_no(m)
    return memberships


@member_login_required
def fanclub_list(request):
    """加入中のファンクラブ一覧 (会員証はここから開く)。"""
    return render(
        request,
        "members/fanclub.html",
        {"memberships": _my_active_memberships(request.member)},
    )


@member_login_required
def fanclub_card(request, creator_slug):
    """デジタル会員証 (会員番号・ティア・加入日・継続月数)。"""
    membership = next(
        (m for m in _my_active_memberships(request.member) if m.creator.slug == creator_slug), None
    )
    if membership is None:
        return redirect("/members/fanclub/")
    return render(request, "members/fanclub_card.html", {"membership": membership})


# --- お気に入りチャンネル (ピン留め) #EPG-04 ---
@member_login_required
@require_POST
def channel_favorite_toggle(request, slug):
    from core.models import Channel

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    fav = ChannelFavorite.objects.filter(member=request.member, channel=channel).first()
    if fav:
        fav.delete()
        pinned = False
    else:
        ChannelFavorite.objects.create(member=request.member, channel=channel)
        pinned = True
    if _is_ajax(request):
        return JsonResponse({"pinned": pinned})
    return redirect(f"/ch/{slug}/")


# --- 視聴リマインド (番組開始前メール) #EPG-03 ---
@member_verified_required
@require_POST
def reminder_toggle(request, program_id):
    from scheduling.models import Program

    program = get_object_or_404(Program, pk=program_id, public_visible=True)
    rem = Reminder.objects.filter(member=request.member, program=program).first()
    if rem:
        rem.delete()
        reminded = False
    else:
        Reminder.objects.create(member=request.member, program=program)
        reminded = True
    if _is_ajax(request):
        return JsonResponse({"reminded": reminded})
    return redirect(f"/program/{program_id}/")


# --- 視聴履歴・続きから (#PERS-02) ---
def _int_arg(request, key) -> int:
    try:
        return max(0, int(request.POST.get(key) or 0))
    except (TypeError, ValueError):
        return 0


@member_login_required
@require_POST
def watch_progress(request, program_id):
    """VOD 再生位置の upsert。プレイヤーが周期/一時停止/離脱で送る (204)。"""
    from scheduling.models import Program

    program = get_object_or_404(Program, pk=program_id, public_visible=True)
    position_ms = _int_arg(request, "position_ms")
    duration_ms = _int_arg(request, "duration_ms") or None
    # 末尾 15s 以内まで見たら完了扱い (続きから対象外にする)
    completed = bool(duration_ms and position_ms >= duration_ms - 15000)
    WatchHistory.objects.update_or_create(
        member=request.member,
        program=program,
        defaults={"position_ms": position_ms, "duration_ms": duration_ms, "completed": completed},
    )
    return HttpResponse(status=204)


@member_login_required
def watch_history(request):
    rows = (
        WatchHistory.objects.filter(member=request.member)
        .select_related("program", "program__channel")
        .order_by("-updated_at")[:60]
    )
    return render(request, "members/history.html", {"rows": rows})


# --- メール確認 (本人確認の1要素) ---
@member_login_required
def verify_email(request):
    if request.member.is_verified:
        return redirect("/members/")
    return render(request, "members/verify_email.html", {})


@member_login_required
@require_POST
def send_email_code(request):
    if request.member.is_verified:
        return redirect("/members/")
    try:
        codes.send_code(request.member, CodePurpose.EMAIL_VERIFY)
        messages.success(request, "確認コードをメールで送信しました。")
    except codes.SendThrottledError as e:
        messages.error(request, f"送信間隔が短すぎます。{e.retry_after}秒後に再度お試しください。")
    except Exception:
        logger.exception("member email code send failed: member=%s", request.member.pk)
        messages.error(request, "メール送信に失敗しました。時間をおいて再度お試しください。")
    return redirect("/members/verify/email/")


@member_login_required
@require_POST
def confirm_email_code(request):
    member = request.member
    if member.is_verified:
        return redirect("/members/")
    if codes.verify_code(member, CodePurpose.EMAIL_VERIFY, request.POST.get("code", "")):
        member.email_verified_at = timezone.now()
        member.save(update_fields=["email_verified_at", "updated_at"])
        messages.success(request, "メールアドレスを確認しました。")
        return redirect("/members/")
    messages.error(request, "確認コードが正しくないか、有効期限が切れています。")
    return redirect("/members/verify/email/")


# --- ログインの第2要素 (2FA) ---
def _complete_or_challenge(request, member):
    """パスワード通過後。two_factor_method により第2要素へ分岐 or 即ログイン。"""
    method = member.two_factor_method
    if method == TwoFactorMethod.TOTP and member.totp_confirmed_at:
        start_pending_2fa(request, member, "totp")
        request.session[_LOGIN_NEXT_KEY] = _safe_next(request)
        return redirect("/members/login/2fa/totp/")
    if method == TwoFactorMethod.EMAIL and member.email_verified_at:
        try:
            codes.send_code(member, CodePurpose.LOGIN_2FA)
        except codes.SendThrottledError:
            pass  # 直近に送信済 → 既存コードで継続
        except Exception:
            logger.exception("login 2fa email send failed: member=%s", member.pk)
        start_pending_2fa(request, member, "email")
        request.session[_LOGIN_NEXT_KEY] = _safe_next(request)
        return redirect("/members/login/2fa/email/")
    login_member(request, member)
    return redirect(_safe_next(request))


def _finish_2fa_login(request, member):
    nxt = request.session.pop(_LOGIN_NEXT_KEY, "/members/")
    login_member(request, member)  # pending をクリアし cycle_key
    return redirect(nxt)


@require_http_methods(["GET", "POST"])
def login_2fa_totp(request):
    member = get_pending_member(request)
    if member is None or get_pending_method(request) != "totp":
        return redirect("/members/login/")
    if request.method == "POST":
        ip = throttle.client_ip(request)
        mkey = str(member.pk)
        # 第2要素にも Cookie 非依存の試行上限を掛ける (M-1: TOTP 総当たり/2FA バイパス防止)。
        if throttle.is_locked(throttle.TOTP, mkey) or throttle.is_locked(throttle.TOTP_IP, ip):
            security_emit(
                "auth.member.2fa",
                outcome="blocked",
                request=request,
                method="totp",
                member_id=member.pk,
            )
            messages.error(request, "試行回数が多すぎます。しばらくしてからお試しください。")
        else:
            step = totp.matched_step(member.totp_secret, request.POST.get("code", ""))
            # step が直近使用ステップ以下ならリプレイ(または時刻ずれ窓での再送)として拒否。
            fresh = step is not None and (
                member.totp_last_step is None or step > member.totp_last_step
            )
            if fresh:
                member.totp_last_step = step
                member.save(update_fields=["totp_last_step", "updated_at"])
                throttle.clear(throttle.TOTP, mkey)
                security_emit(
                    "auth.member.2fa",
                    outcome="success",
                    request=request,
                    method="totp",
                    member_id=member.pk,
                )
                return _finish_2fa_login(request, member)
            throttle.record_failure(throttle.TOTP, mkey)
            throttle.record_failure(throttle.TOTP_IP, ip)
            security_emit(
                "auth.member.2fa",
                outcome="fail",
                request=request,
                method="totp",
                member_id=member.pk,
            )
            messages.error(request, "コードが正しくありません。")
    return render(request, "members/login_2fa_totp.html", {})


@require_http_methods(["GET", "POST"])
def login_2fa_email(request):
    member = get_pending_member(request)
    if member is None or get_pending_method(request) != "email":
        return redirect("/members/login/")
    if request.method == "POST":
        if request.POST.get("resend"):
            try:
                codes.send_code(member, CodePurpose.LOGIN_2FA)
                messages.success(request, "確認コードを再送しました。")
            except codes.SendThrottledError as e:
                messages.error(request, f"{e.retry_after}秒後に再度お試しください。")
            except Exception:
                logger.exception("login 2fa email resend failed: member=%s", member.pk)
                messages.error(request, "メール送信に失敗しました。")
            return redirect("/members/login/2fa/email/")
        if codes.verify_code(member, CodePurpose.LOGIN_2FA, request.POST.get("code", "")):
            return _finish_2fa_login(request, member)
        messages.error(request, "コードが正しくないか、有効期限が切れています。")
    return render(request, "members/login_2fa_email.html", {"email": member.email})


# --- 未認証ゲートの着地ページ (member_verified_required のリダイレクト先) ---
@member_login_required
def verify_required(request):
    if request.member.is_verified:
        return redirect("/members/")
    return render(request, "members/verify_required.html", {})


# --- 2FA 設定 (認証アプリ登録 / メール2FA 有効化 / 解除) ---
@member_login_required
def two_factor_settings(request):
    return render(request, "members/two_factor.html", {})


@member_login_required
@require_http_methods(["GET", "POST"])
def totp_setup(request):
    member = request.member
    if member.totp_confirmed_at:
        return redirect("/members/2fa/")
    if request.method == "POST":
        secret = request.session.get(_TOTP_SETUP_KEY)
        if secret and totp.verify_totp(secret, request.POST.get("code", "")):
            member.totp_secret = secret
            member.totp_confirmed_at = timezone.now()
            member.two_factor_method = TwoFactorMethod.TOTP
            member.save(
                update_fields=[
                    "totp_secret",
                    "totp_confirmed_at",
                    "two_factor_method",
                    "updated_at",
                ]
            )
            request.session.pop(_TOTP_SETUP_KEY, None)
            messages.success(
                request, "認証アプリを登録しました。次回ログインからコードが必要です。"
            )
            return redirect("/members/2fa/")
        messages.error(request, "コードが正しくありません。アプリの最新コードを入力してください。")
    # GET、または確認失敗時: 暫定 secret を確保 (再読込でも QR を安定させる)
    secret = request.session.get(_TOTP_SETUP_KEY)
    if not secret:
        secret = totp.new_secret()
        request.session[_TOTP_SETUP_KEY] = secret
    uri = totp.provisioning_uri(member.email, secret)
    return render(
        request, "members/totp_setup.html", {"secret": secret, "qr_svg": totp.qr_svg(uri)}
    )


@member_login_required
@require_POST
def email_2fa_enable(request):
    member = request.member
    if not member.email_verified_at:
        messages.error(request, "先にメールアドレスを確認してください。")
        return redirect("/members/verify/email/")
    member.two_factor_method = TwoFactorMethod.EMAIL
    member.save(update_fields=["two_factor_method", "updated_at"])
    messages.success(request, "ログイン時のメール2段階認証を有効にしました。")
    return redirect("/members/2fa/")


@member_login_required
@require_POST
def two_factor_disable(request):
    """2FA を無効化 (パスワード再入力)。TOTP 秘密も破棄する。"""
    member = request.member
    if not member.check_password(request.POST.get("password", "")):
        messages.error(request, "パスワードが正しくありません。")
        return redirect("/members/2fa/")
    member.two_factor_method = TwoFactorMethod.NONE
    member.totp_secret = None
    member.totp_confirmed_at = None
    member.save(
        update_fields=["two_factor_method", "totp_secret", "totp_confirmed_at", "updated_at"]
    )
    messages.success(request, "2段階認証を無効にしました。")
    return redirect("/members/2fa/")
