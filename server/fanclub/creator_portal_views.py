# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""creator.* セルフサービス画面: dashboard / ティア / 番組(閲覧) / 投稿CRUD / 会員集計 / 枠契約(閲覧)。

すべて creator_login_required。投稿CRUDは series が自分の Creator に紐付くことを都度検証し、
他クリエイターの Series は 404 にする (テナント境界)。
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from fanclub import stripe_gateway as gw
from fanclub.creator_decorators import creator_login_required
from fanclub.models import (
    CreatorMembership,
    CreatorSeriesLink,
    CreatorTier,
    MembershipStatus,
    SlotContract,
    SlotContractStatus,
)
from fanclub.services import ensure_free_tier, tier_is_joinable

logger = logging.getLogger(__name__)


def _my_creator(request):
    return request.creator_account.creator


def _creator_base_url() -> str:
    return (settings.ICSTV_CREATOR_BASE_URL or "").rstrip("/")


@creator_login_required
def dashboard(request):
    creator = _my_creator(request)
    series_count = creator.series_links.count()
    member_count = creator.memberships.filter(status=MembershipStatus.ACTIVE).count()
    active_contract_count = creator.slot_contracts.filter(status="active").count()
    return render(
        request,
        "creator/dashboard.html",
        {
            "creator": creator,
            "series_count": series_count,
            "member_count": member_count,
            "active_contract_count": active_contract_count,
        },
    )


@creator_login_required
@require_POST
def connect_stripe(request):
    """有料ティアの入金先設定(Stripe Connect Express オンボーディング)を開始/再開する。"""
    creator = _my_creator(request)
    base = _creator_base_url()
    try:
        account_id = gw.ensure_connect_account(creator)
        if creator.stripe_connect_account_id != account_id:
            creator.stripe_connect_account_id = account_id
            creator.save(update_fields=["stripe_connect_account_id"])
        url = gw.create_account_link(
            account_id=account_id,
            refresh_url=f"{base}/dashboard/",
            return_url=f"{base}/dashboard/",
        )
    except gw.StripeNotConfiguredError:
        messages.error(request, "決済連携は現在利用できません。")
        return redirect("/dashboard/")
    except Exception:
        logger.exception("stripe connect onboarding failed: creator=%s", creator.pk)
        messages.error(request, "Stripe連携の開始に失敗しました。時間をおいてお試しください。")
        return redirect("/dashboard/")
    return redirect(url)


# --------------------------------------------------------------------------- #
#  ティア                                                                     #
# --------------------------------------------------------------------------- #


@creator_login_required
def tiers(request):
    creator = _my_creator(request)
    ensure_free_tier(creator)
    tier_list = list(creator.tiers.order_by("level"))
    joinable_tier_ids = {t.id for t in tier_list if tier_is_joinable(t)}
    return render(
        request,
        "creator/tiers.html",
        {"creator": creator, "tiers": tier_list, "joinable_tier_ids": joinable_tier_ids},
    )


@creator_login_required
@require_POST
def tier_create(request):
    creator = _my_creator(request)
    try:
        level = int(request.POST.get("level", ""))
        price = int(request.POST.get("price_minor", ""))
    except ValueError:
        messages.error(request, "level/月額は数値で入力してください。")
        return redirect("/tiers/")
    if level == 0:
        messages.error(request, "level0(無料)は常設のため新規作成できません。")
        return redirect("/tiers/")
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "ティア名は必須です。")
        return redirect("/tiers/")
    try:
        CreatorTier.objects.create(creator=creator, level=level, name=name, price_minor=price)
    except IntegrityError:
        messages.error(request, "このlevelは既に使われています。")
    else:
        messages.success(request, "ティアを作成しました。")
    return redirect("/tiers/")


@creator_login_required
@require_POST
def tier_toggle_active(request, tier_id: int):
    creator = _my_creator(request)
    tier = get_object_or_404(CreatorTier, pk=tier_id, creator=creator)
    tier.is_active = not tier.is_active
    tier.save(update_fields=["is_active"])
    return redirect("/tiers/")


# --------------------------------------------------------------------------- #
#  番組 (閲覧のみ、紐付けはstudio専管)                                          #
# --------------------------------------------------------------------------- #


@creator_login_required
def series_list(request):
    creator = _my_creator(request)
    links = creator.series_links.select_related("series", "series__channel").order_by(
        "series__title"
    )
    return render(request, "creator/series_list.html", {"creator": creator, "links": links})


def _my_series_or_404(request, series_id: int):
    """series_id が自分の Creator に紐付いているか検証し、Series インスタンスを返す (他は404)。"""
    creator = _my_creator(request)
    link = get_object_or_404(CreatorSeriesLink, series_id=series_id, creator=creator)
    return link.series


# --------------------------------------------------------------------------- #
#  投稿 (SeriesPost) CRUD                                                     #
# --------------------------------------------------------------------------- #


def _tier_choices(creator):
    """公開範囲セレクトの選択肢: 無料会員以上(0) + 自分の有効な有料ティア。"""
    paid = list(creator.tiers.filter(is_active=True, level__gt=0).order_by("level"))
    return paid


@creator_login_required
def posts_list(request, series_id: int):
    series = _my_series_or_404(request, series_id)
    posts = series.posts.order_by("-created_at")
    return render(request, "creator/posts_list.html", {"series": series, "posts": posts})


def _apply_post_form(request, series, post):
    from scheduling.models import SeriesPostKind

    kind = request.POST.get("kind", SeriesPostKind.ARTICLE)
    title = (request.POST.get("title") or "").strip()
    if not title:
        return "タイトルは必須です。"
    post.kind = kind
    post.title = title
    post.body = request.POST.get("body", "")
    post.media_url = request.POST.get("media_url", "")
    post.form_url = request.POST.get("form_url", "")
    level_raw = request.POST.get("fc_required_level", "")
    if level_raw == "":
        post.fc_required_level = None
    else:
        try:
            level = int(level_raw)
        except ValueError:
            return "公開範囲の指定が不正です。"
        if level != 0 and level not in {
            t.level for t in _tier_choices(series.fanclub_link.creator)
        }:
            return "選択した公開範囲は無効です(削除/無効化されたティアの可能性があります)。"
        post.fc_required_level = level
    was_published = post.is_published
    post.is_published = request.POST.get("is_published") == "on"
    if post.is_published and not was_published:
        post.published_at = timezone.now()
    post.series = series
    return None


@creator_login_required
@require_http_methods(["GET", "POST"])
def post_new(request, series_id: int):
    from scheduling.models import SeriesPost

    series = _my_series_or_404(request, series_id)
    creator = _my_creator(request)
    if request.method == "POST":
        post = SeriesPost(series=series)
        err = _apply_post_form(request, series, post)
        if err:
            messages.error(request, err)
        else:
            post.save()
            messages.success(request, "投稿を作成しました。")
            return redirect(f"/series/{series.id}/posts/")
    choices = _tier_choices(creator)
    return render(
        request,
        "creator/post_form.html",
        {
            "series": series,
            "post": None,
            "tier_choices": choices,
            "joinable_tier_ids": {t.id for t in choices if tier_is_joinable(t)},
        },
    )


@creator_login_required
@require_http_methods(["GET", "POST"])
def post_edit(request, series_id: int, post_id: int):
    from scheduling.models import SeriesPost

    series = _my_series_or_404(request, series_id)
    creator = _my_creator(request)
    post = get_object_or_404(SeriesPost, pk=post_id, series=series)
    if request.method == "POST":
        err = _apply_post_form(request, series, post)
        if err:
            messages.error(request, err)
        else:
            post.save()
            messages.success(request, "投稿を更新しました。")
            return redirect(f"/series/{series.id}/posts/")
    choices = _tier_choices(creator)
    return render(
        request,
        "creator/post_form.html",
        {
            "series": series,
            "post": post,
            "tier_choices": choices,
            "joinable_tier_ids": {t.id for t in choices if tier_is_joinable(t)},
        },
    )


@creator_login_required
@require_POST
def post_delete(request, series_id: int, post_id: int):
    from scheduling.models import SeriesPost

    series = _my_series_or_404(request, series_id)
    post = get_object_or_404(SeriesPost, pk=post_id, series=series)
    post.delete()
    messages.success(request, "投稿を削除しました。")
    return redirect(f"/series/{series.id}/posts/")


# --------------------------------------------------------------------------- #
#  会員 (集計のみ、PII一覧は出さない)                                          #
# --------------------------------------------------------------------------- #


@creator_login_required
def members_summary(request):
    creator = _my_creator(request)
    qs = CreatorMembership.objects.filter(creator=creator, status=MembershipStatus.ACTIVE)
    by_level: dict[int, int] = {}
    for row in qs.values("tier__level"):
        lvl = row["tier__level"]
        by_level[lvl] = by_level.get(lvl, 0) + 1
    total = sum(by_level.values())
    return render(
        request,
        "creator/members_summary.html",
        {"creator": creator, "total": total, "by_level": sorted(by_level.items())},
    )


# --------------------------------------------------------------------------- #
#  枠契約 (閲覧 + Stripe Billing 開始/管理)                                    #
# --------------------------------------------------------------------------- #


@creator_login_required
def contracts_list(request):
    creator = _my_creator(request)
    contracts = creator.slot_contracts.order_by("-starts_on")
    return render(
        request, "creator/contracts_list.html", {"creator": creator, "contracts": contracts}
    )


@creator_login_required
@require_POST
def contract_checkout(request, contract_id: int):
    """枠サブスクの支払いを開始する。Stripe Checkout(プラットフォーム直接課金)へ誘導する。

    実際の状態遷移(draft→active)は Webhook のみが行う(#27 Part 2、CreatorMembership の
    checkout と同じ規律)。契約は studio がスタッフ側で作成する前提のため、ここでは
    既存の draft 契約の支払い開始のみを扱う(新規契約の作成はしない)。
    """
    creator = _my_creator(request)
    contract = get_object_or_404(SlotContract, pk=contract_id, creator=creator)
    if contract.stripe_subscription_id:
        messages.info(
            request, "既にお支払い手続き済みです。変更・解約は「お支払いの管理」から行えます。"
        )
        return redirect("/contracts/")
    if contract.status != SlotContractStatus.DRAFT:
        messages.error(request, "この契約は現在お支払い手続きの対象ではありません。")
        return redirect("/contracts/")
    base = _creator_base_url()
    try:
        url = gw.create_contract_checkout_session(
            customer_id=gw.ensure_contract_customer(creator, contract),
            monthly_fee_minor=contract.monthly_fee_minor,
            title=contract.title,
            success_url=f"{base}/contracts/?slot_success=1",
            cancel_url=f"{base}/contracts/?slot_canceled=1",
            contract_id=contract.pk,
        )
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception("slot contract checkout failed: contract=%s", contract.pk)
        messages.error(request, "決済ページの作成に失敗しました。時間をおいてお試しください。")
        return redirect("/contracts/")
    return redirect(url)


@creator_login_required
@require_POST
def contract_portal(request, contract_id: int):
    """枠サブスクの解約/カード変更。Stripe カスタマーポータルへ誘導する。"""
    creator = _my_creator(request)
    contract = get_object_or_404(SlotContract, pk=contract_id, creator=creator)
    if not contract.stripe_customer_id:
        return redirect("/contracts/")
    base = _creator_base_url()
    try:
        url = gw.create_portal_session(
            customer_id=contract.stripe_customer_id, return_url=f"{base}/contracts/"
        )
    except gw.StripeNotConfiguredError:
        return HttpResponse("決済は現在利用できません。", status=503)
    except Exception:
        logger.exception("slot contract portal failed: contract=%s", contract.pk)
        messages.error(request, "管理ページを開けませんでした。")
        return redirect("/contracts/")
    return redirect(url)


# --------------------------------------------------------------------------- #
#  分配元帳 (閲覧のみ、#27 Phase B)                                            #
# --------------------------------------------------------------------------- #

_SETTLEMENT_PAGE_SIZE = 100


@creator_login_required
def settlements_list(request):
    creator = _my_creator(request)
    qs = creator.settlements.order_by("-created_at")
    items = list(qs[:_SETTLEMENT_PAGE_SIZE])
    # **合計は通貨ごとに出す。** 通貨をまたいで足すと「円とドルを足した数字」になり、
    # 一見それらしい値が出るぶん誤りに気付けない。現状は jpy 単一なので実質 1 行だが、
    # 外貨が 1 件でも混ざった時点で自動的に分かれる。
    totals_by_currency = list(
        qs.values("currency")
        .annotate(
            total_gross=Sum("gross_amount_minor"),
            total_fee=Sum("application_fee_minor"),
            total_net=Sum("net_amount_minor"),
        )
        .order_by("currency")
    )
    return render(
        request,
        "creator/settlements_list.html",
        {
            "creator": creator,
            "items": items,
            "totals_by_currency": totals_by_currency,
        },
    )


# --------------------------------------------------------------------------- #
#  YouTube 宛先 (#27 Part B)                                                  #
# --------------------------------------------------------------------------- #


# 「ページ設定」画面が編集する Creator の列 (#27 §10.2)。POST 取り込み・full_clean の対象範囲・
# save(update_fields) の3箇所で同じ集合を使い、増減時の取りこぼしを防ぐ。
_PROFILE_FIELDS = (
    "description",
    "avatar_url",
    "cover_url",
    "theme_color",
    "sns_x_url",
    "sns_youtube_url",
    "sns_instagram_url",
    "website_url",
)


@creator_login_required
@require_http_methods(["GET", "POST"])
def profile_edit(request):
    """公開ファンクラブページ (/fc/<slug>/) のプロフィール編集 (#27 §10.2)。

    theme_color は full_clean でモデルの RegexValidator を通す (描画側の
    services.safe_theme_color と二重の防壁)。画像は URL 貼り付け方式
    (SeriesPost.media_url と同じ運用、アップロード基盤は持たない)。
    """
    creator = _my_creator(request)
    if request.method == "POST":
        for field in _PROFILE_FIELDS:
            setattr(creator, field, (request.POST.get(field) or "").strip())
        chat_raw = request.POST.get("chat_required_level", "")
        if chat_raw == "off":
            creator.chat_required_level = None
        else:
            try:
                chat_level = int(chat_raw)
            except ValueError:
                chat_level = 0
            if chat_level != 0 and chat_level not in {
                t.level for t in creator.tiers.filter(is_active=True, level__gt=0)
            }:
                chat_level = 0  # 無効化されたティアを指したままにしない
            creator.chat_required_level = chat_level
        # 検証対象はこの画面が編集した列だけに絞る。full_clean をフル実行すると、
        # 別画面が入れた値 (Stripe/特商法/YouTube 系) の不備でこの保存が巻き添えで
        # 失敗し、原因の分からないエラーがクリエイターに出てしまう。
        edited = {*_PROFILE_FIELDS, "chat_required_level"}
        try:
            creator.full_clean(
                exclude=[f.name for f in creator._meta.fields if f.name not in edited],
                validate_unique=False,
            )
        except ValidationError as exc:
            for field, errs in exc.message_dict.items():
                messages.error(request, f"{field}: {'/'.join(errs)}")
            return redirect("/profile/")
        creator.save(update_fields=[*_PROFILE_FIELDS, "chat_required_level"])
        messages.success(request, "プロフィールを保存しました。")
        return redirect("/profile/")
    public_base = (getattr(settings, "ICSTV_PUBLIC_BASE_URL", "") or "").rstrip("/")
    return render(
        request,
        "creator/profile_edit.html",
        {
            "creator": creator,
            "public_page_url": f"{public_base}/fc/{creator.slug}/",
            "chat_tiers": list(creator.tiers.filter(is_active=True, level__gt=0).order_by("level")),
        },
    )


@creator_login_required
@require_http_methods(["GET", "POST"])
def youtube_destination(request):
    """自分の YouTube チャンネル宛シミュルキャストの宛先 (ストリームキー) を設定する。

    docs/fanclub.md §5.5・§6.5。実際にシミュルキャストされるかは運営が枠契約で
    youtube_destination=creator_channel を選んでいるか次第 (studio 専管、ここでは変更しない)。
    ストリームキーは秘密値のため画面には再表示しない (空欄送信=既存値を維持)。
    """
    creator = _my_creator(request)
    if request.method == "POST":
        ingest_url = (request.POST.get("ingest_url") or "").strip()
        stream_key = (request.POST.get("stream_key") or "").strip()
        if not ingest_url:
            messages.error(request, "配信先 URL は必須です。")
            return redirect("/youtube/")
        creator.youtube_destination_ingest_url = ingest_url
        update_fields = ["youtube_destination_ingest_url"]
        if stream_key:
            creator.youtube_destination_stream_key = stream_key
            update_fields.append("youtube_destination_stream_key")
        creator.save(update_fields=update_fields)
        messages.success(request, "YouTube宛先を保存しました。")
        return redirect("/youtube/")
    return render(request, "creator/youtube_destination.html", {"creator": creator})
