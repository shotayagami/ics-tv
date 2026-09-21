# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開シリーズ詳細 API。/series/{series_id} — 番組紹介サイトページ用。"""

from datetime import timedelta

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from api.schemas import AudienceSubmitIn, AudienceSubmitOut, SeriesDetailOut, SeriesPostDetailOut

router = Router(tags=["series"])

_WD = ["月", "火", "水", "木", "金", "土", "日"]


def _jp_dt(dt) -> str:
    lt = timezone.localtime(dt)
    return f"{lt.month}/{lt.day}({_WD[lt.weekday()]}) {lt:%H:%M}"


def _jp_date(dt) -> str:
    if dt is None:
        return ""
    lt = timezone.localtime(dt)
    return f"{lt.month}/{lt.day}({_WD[lt.weekday()]})"


@router.get("/series/{series_id}", response=SeriesDetailOut, auth=None)
def series_detail(request: HttpRequest, series_id: int):
    from django.shortcuts import get_object_or_404

    from fanclub import services as fc_services
    from scheduling import vod as vod_mod
    from scheduling.models import Program, Series

    series = get_object_or_404(Series, pk=series_id, is_active=True)
    now = timezone.now()
    member = getattr(request, "member", None) or None
    creator = fc_services.creator_for_series(series)

    programs = (
        Program.objects.filter(series=series, public_visible=True)
        .select_related("channel", "asset")
        .order_by("-start_at")[:60]
    )
    vod_qs = vod_mod.available_vod_qs(now)
    vod_ids = set(vod_qs.values_list("id", flat=True))

    episodes = []
    for p in programs:
        if p.start_at <= now <= p.end_at:
            state = "live"
        elif p.start_at > now:
            state = "upcoming"
        else:
            state = "aired"
        asset = getattr(p, "asset", None)
        duration = asset.duration_display if asset and getattr(asset, "duration_ms", None) else ""
        episodes.append(
            {
                "id": p.id,
                "title": p.title,
                "start_display": _jp_dt(p.start_at),
                "state": state,
                "thumb_url": p.thumb_url or "",
                "vod_url": f"/vod/{p.id}/" if p.id in vod_ids else "",
                "program_url": f"/program/{p.id}/",
                "duration": duration,
            }
        )

    posts = []
    for post in series.posts.filter(is_published=True):
        reason = fc_services.fc_gate_reason(post.fc_required_level, member, creator)
        posts.append(
            {
                "id": post.id,
                "kind": post.kind,
                "title": post.title,
                "published_at": _jp_date(post.published_at),
                "campaign_end": _jp_dt(post.campaign_end) if post.campaign_end else "",
                "locked": bool(reason),
                "gate_reason": reason,
            }
        )

    # enabled なフォームのみ公開。campaign は受付期間内のみ。
    audience_forms = []
    for af in series.audience_forms.filter(enabled=True).order_by("pk"):
        if not af.is_open(now):
            continue
        audience_forms.append(
            {
                "id": af.id,
                "kind": af.kind,
                "title": af.title,
                "description": af.description or "",
                "requires_login": af.requires_login,
                "fields": af.fields or [],
                "success_message": af.success_message or "送信しました。ありがとうございました。",
                "prize": af.prize or "",
                "ends_display": _jp_dt(af.ends_at) if af.ends_at else "",
            }
        )

    return {
        "id": series.id,
        "title": series.title,
        "slug": series.slug or "",
        "description": series.description or "",
        "genre": series.genre or "",
        "cast": series.cast or "",
        "thumbnail_url": series.thumbnail_url or "",
        "channel_name": series.channel.name,
        "public_url": series.public_url,
        "x_handle_display": series.x_handle_display,
        "x_url": series.x_url,
        "x_hashtag_display": series.x_hashtag_display,
        "hashtag_url": series.hashtag_url,
        "episodes": episodes,
        "posts": posts,
        "forms": audience_forms,
    }


@router.get("/series/{series_id}/posts/{post_id}", response=SeriesPostDetailOut, auth=None)
def series_post_detail(request: HttpRequest, series_id: int, post_id: int):
    """シリーズ投稿の本文 (#MOBILE-02)。core.views.public_series_post と同じゲート判定。

    ロック中は本文/メディア/フォーム URL を一切返さない (SSR のロックページと同じ漏えい防止)。
    タイトル/掲載日は一覧と同様に常に見せる (発見性/コンバージョン導線)。
    """
    from django.shortcuts import get_object_or_404

    from fanclub import services as fc_services
    from scheduling.models import Series, SeriesPost

    series = get_object_or_404(Series, pk=series_id, is_active=True)
    post = get_object_or_404(SeriesPost, pk=post_id, series=series, is_published=True)
    member = getattr(request, "member", None)
    creator = fc_services.creator_for_series(series)
    reason = fc_services.fc_gate_reason(post.fc_required_level, member, creator)
    locked = bool(reason)
    return {
        "id": post.pk,
        "series_id": series.pk,
        "series_title": series.title,
        "kind": post.kind,
        "title": post.title,
        "published_at": _jp_date(post.published_at),
        "locked": locked,
        "gate": reason,
        "fc_creator_slug": creator.slug if creator else "",
        "body_html": "" if locked else post.body,
        "media_url": "" if locked else post.media_url,
        "form_url": "" if locked else post.form_url,
        "campaign_end": _jp_dt(post.campaign_end) if post.campaign_end else "",
    }


_SUBMIT_COOLDOWN = 30  # 秒
_SUBMIT_HOURLY_LIMIT = 10


@router.post("/forms/{form_id}/submit", response=AudienceSubmitOut, auth=None)
def audience_submit(request: HttpRequest, form_id: int, payload: AudienceSubmitIn):
    """投書/キャンペーン応募を受け取り DB に保存する。

    - requires_login かつ 未ログイン → 401 (gate=login)
    - campaign かつ 受付期間外 → 410
    - rate-limit 超過 → 429
    - フィールド検証失敗 → 422
    """
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    from scheduling.models import AudienceForm, AudienceSubmission
    from scheduling.services import PayloadValidationError, validate_payload

    now = timezone.now()
    form = AudienceForm.objects.filter(pk=form_id, enabled=True).select_related("series").first()
    if form is None:
        raise HttpError(404, "フォームが見つかりません。")

    if not form.is_open(now):
        raise HttpError(410, "このフォームは現在受付していません。")

    # SimpleLazyObject を force-evaluate して None/Member に解決する
    member = getattr(request, "member", None) or None

    if form.requires_login and member is None:
        return AudienceSubmitOut(ok=False, gate="login", message="ログインが必要です。")

    # rate-limit: 同一 member or IP で直近 30s / 1h 上限
    ip = request.META.get("REMOTE_ADDR")
    qs = AudienceSubmission.objects.filter(form=form)
    qs_target = qs.filter(member=member) if member else qs.filter(ip=ip, member__isnull=True)

    last = qs_target.order_by("-created_at").first()
    if last and (now - last.created_at).total_seconds() < _SUBMIT_COOLDOWN:
        raise HttpError(429, "投稿が早すぎます。少し待ってからお試しください。")
    recent_count = qs_target.filter(created_at__gte=now - timedelta(hours=1)).count()
    if recent_count >= _SUBMIT_HOURLY_LIMIT:
        raise HttpError(429, "1時間あたりの投稿上限に達しました。")

    # ペイロード検証
    data = payload.payload or {}
    try:
        validate_payload(form, data)
    except PayloadValidationError as exc:
        raise HttpError(422, str(exc)) from exc

    # 送信者名/メール を payload の既知キーから抽出
    name_keys = {"radio_name", "name", "お名前", "ラジオネーム"}
    email_keys = {"email", "メールアドレス", "mail"}
    submitter_name = ""
    submitter_email = ""
    for fdef in form.fields:
        key = fdef.get("key", "")
        val = str(data.get(key, "")).strip()
        if not val:
            continue
        if key in name_keys or fdef.get("label", "") in name_keys:
            submitter_name = submitter_name or val
        if fdef.get("type") == "email" or key in email_keys:
            submitter_email = submitter_email or val
    if member and not submitter_name:
        submitter_name = member.nickname or ""
    if member and not submitter_email:
        submitter_email = member.email or ""

    sub = AudienceSubmission.objects.create(
        form=form,
        member=member,
        payload=data,
        submitter_name=submitter_name[:200],
        submitter_email=submitter_email[:200],
        ip=ip,
    )

    # スタッフへの通知メール
    if form.notify_email.strip():
        try:
            body = render_to_string(
                "scheduling/email/audience_submission.txt",
                {"form": form, "sub": sub, "data": data},
            )
            from django.conf import settings

            recipients = [e.strip() for e in form.notify_email.split(",") if e.strip()]
            send_mail(
                subject=f"[投稿通知] {form.series.title} — {form.title}",
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipients,
                fail_silently=True,
            )
        except Exception:
            pass  # 通知失敗は投稿成功に影響させない

    return AudienceSubmitOut(ok=True, message=form.success_message or "送信しました。")
