# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""staff 専用 admin UI: channel 設定 + YouTube OAuth 接続フロー (docs/ui.md §A)。

Google OAuth Authorization Code Flow:
1. /admin-ui/ch/<slug>/youtube/connect → consent URL へ redirect
2. ユーザ承諾 → /admin-ui/oauth/callback?code=...&state=...
3. code を access_token + refresh_token に交換 → YoutubeCredential に upsert

ICSTV_OAUTH_CLIENT_ID/SECRET (Google Cloud Console で取得) が未設定なら 503。
"""

from __future__ import annotations

import contextlib
import os
import re

import httpx
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_slug
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from google_auth_oauthlib.flow import Flow
from googleapiclient.errors import HttpError

from core.models import Channel
from medialib.models import Asset, AssetKind, FillerPlaylist, NormalizeStatus
from youtube.forms import YoutubeBroadcastPresetForm
from youtube.models import (
    ProgramBroadcast,
    YoutubeBroadcastPreset,
    YoutubeConfig,
    YoutubeCredential,
    YoutubeSlot,
    YtSlotStatus,
    default_manual_checklist,
)

_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


class OAuthNotConfiguredError(RuntimeError):
    pass


def _build_flow(request, *, state: str | None = None) -> Flow:
    client_id = os.environ.get("ICSTV_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("ICSTV_OAUTH_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise OAuthNotConfiguredError("ICSTV_OAUTH_CLIENT_ID/SECRET が未設定")
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
    flow.redirect_uri = request.build_absolute_uri(reverse("core:youtube_oauth_callback"))
    return flow


@staff_member_required
def channel_settings(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug)
    cred = YoutubeCredential.objects.filter(channel=channel).first()
    yt_cfg = YoutubeConfig.objects.filter(channel=channel).first()
    connected = bool(cred and cred.refresh_token)
    oauth_configured = bool(
        os.environ.get("ICSTV_OAUTH_CLIENT_ID") and os.environ.get("ICSTV_OAUTH_CLIENT_SECRET")
    )
    # フィラー playlist / スレート素材の割当候補 (基本操作を studio で完結させる。緊急時は admin)。
    # スレートは prefetch+送出に R2 が要るため正規化済 (READY) のみ。filler/bumper/slate 等 (番組/CM 以外)。
    filler_playlists = list(FillerPlaylist.objects.order_by("name"))
    slate_candidates = list(
        Asset.objects.exclude(kind__in=[AssetKind.PROGRAM, AssetKind.CM])
        .filter(normalize_status=NormalizeStatus.READY)
        .order_by("title")
    )
    # 朝夕時計: 現在の窓を「HH:MM-HH:MM」行テキストに、未設定時の既定窓を案内表示用に整形。
    clock_windows_text = "\n".join(
        f"{w.get('start', '')}-{w.get('end', '')}"
        for w in (channel.clock_windows or [])
        if isinstance(w, dict)
    )
    clock_default_windows = " / ".join(
        f"{w.get('start', '')}-{w.get('end', '')}"
        for w in getattr(settings, "ICSTV_CLOCK_WINDOWS", [])
        if isinstance(w, dict)
    )
    # 放送時間帯: 現在の設定を「HH:MM-HH:MM」行テキストに整形。
    broadcast_windows_text = "\n".join(
        f"{w.get('start', '')}-{w.get('end', '')}"
        for w in (channel.broadcast_windows or [])
        if isinstance(w, dict)
    )
    return render(
        request,
        "admin_ui/channel_settings.html",
        {
            "channel": channel,
            "cred": cred,
            "yt_cfg": yt_cfg,
            "connected": connected,
            "oauth_configured": oauth_configured,
            "filler_playlists": filler_playlists,
            "slate_candidates": slate_candidates,
            "clock_windows_text": clock_windows_text,
            "clock_default_windows": clock_default_windows,
            "broadcast_windows_text": broadcast_windows_text,
        },
    )


def _pk_or_none(val: str | None) -> int | None:
    s = val or ""
    return int(s) if s.isdigit() else None


@staff_member_required
@require_POST
def set_channel_media(request, slug: str) -> HttpResponse:
    """チャンネルの既定フィラー (FillerPlaylist) と スレート/exposure_policy 案内素材 (Asset) を保存。

    基本操作を studio で完結させる (Django admin は緊急時用)。空選択は未設定 (NULL) に戻す。
    不正/存在しない id は None 扱い (dangling FK を作らない)。site_only_filler/members_filler は
    exposure_policy (#27) の YTミラー案内フィラー2種 (docs/site-only-broadcast.md §4.7)。
    """
    channel = get_object_or_404(Channel, slug=slug)
    filler_pk = _pk_or_none(request.POST.get("default_filler"))
    slate_pk = _pk_or_none(request.POST.get("slate_asset"))
    site_only_filler_pk = _pk_or_none(request.POST.get("site_only_filler"))
    members_filler_pk = _pk_or_none(request.POST.get("members_filler"))
    channel.default_filler = (
        FillerPlaylist.objects.filter(pk=filler_pk).first() if filler_pk else None
    )
    channel.slate_asset = Asset.objects.filter(pk=slate_pk).first() if slate_pk else None
    channel.site_only_filler = (
        Asset.objects.filter(pk=site_only_filler_pk).first() if site_only_filler_pk else None
    )
    channel.members_filler = (
        Asset.objects.filter(pk=members_filler_pk).first() if members_filler_pk else None
    )
    channel.save(
        update_fields=["default_filler", "slate_asset", "site_only_filler", "members_filler"]
    )
    return redirect("core:channel_settings", slug=channel.slug)


@staff_member_required
@require_POST
def upload_chime_sound(request) -> HttpResponse:
    """速報チャイム音源をライブラリ (ChimeSound・局共通) に追加 (studio 設定の multipart POST)。

    POST: name (任意ラベル) + file (音声)。content-addressed key で R2 保存し、配布は standing
    manifest 経由でライブラリ全体を agent が pin+DL する (core.chimes.add_to_library)。検証 NG 400。
    """
    from core import chimes

    f = request.FILES.get("file")
    if not f:
        return HttpResponse("音声ファイルを選択してください", status=400)
    try:
        chimes.add_to_library(request.POST.get("name", ""), f)
    except chimes.ChimeError as e:
        return HttpResponse(str(e), status=400)
    return HttpResponse("音源を追加しました", status=200)


@staff_member_required
@require_POST
def delete_chime_sound(request, sound_id: int) -> HttpResponse:
    """ライブラリから音源を削除 (R2 も消す)。選択中だった場合は自動的に未選択へ戻る (SET_NULL)。"""
    from core import chimes

    chimes.remove_from_library(sound_id)
    return HttpResponse("音源を削除しました", status=200)


@staff_member_required
def preview_chime_sound(request, sound_id: int) -> HttpResponse:
    """試聴用。ライブラリ音源の短命 presigned GET URL へ 302 リダイレクトする。"""
    from core import r2
    from core.models import ChimeSound

    sound = get_object_or_404(ChimeSound, pk=sound_id)
    return redirect(r2.presign_get(sound.r2_key, expires=300))


@staff_member_required
@require_POST
def select_channel_chime(request, slug: str) -> HttpResponse:
    """カテゴリ (eew|weather|general) に使うライブラリ音源を選ぶ (per-channel)。

    POST: category + sound (ChimeSound id・空=未選択で既定へフォールバック)。
    """
    from core import chimes

    channel = get_object_or_404(Channel, slug=slug)
    sound_id = _pk_or_none(request.POST.get("sound"))
    try:
        chimes.select(channel, request.POST.get("category", ""), sound_id)
    except chimes.ChimeError as e:
        return HttpResponse(str(e), status=400)
    return HttpResponse("チャイムを設定しました", status=200)


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
# 放送時間帯終端: HH:MM または 24:00 (翌日 00:00) を許容。
_HHMM_END_RE = re.compile(r"^(([01]?\d|2[0-3]):[0-5]\d|24:00)$")


@staff_member_required
@require_POST
def set_clock_overlay(request, slug: str) -> HttpResponse:
    """朝・夕の左上時計 (daypart) の有効化 + 表示時間帯を保存。

    windows は 1 行 1 窓の「HH:MM-HH:MM」テキスト (JST)。空なら settings.ICSTV_CLOCK_WINDOWS にフォールバック。
    end<=start は翌日跨ぎ (resolver が解釈)。形式不正は messages で通知し保存しない。
    """
    channel = get_object_or_404(Channel, slug=slug)
    enabled = bool(request.POST.get("clock_overlay_enabled"))
    windows: list[dict[str, str]] = []
    for line in (request.POST.get("clock_windows") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        start, _, end = line.partition("-")
        start, end = start.strip(), end.strip()
        if not (_HHMM_RE.match(start) and _HHMM_RE.match(end)):
            messages.error(request, f"時間帯は HH:MM-HH:MM 形式で入力してください: {line!r}")
            return redirect("core:channel_settings", slug=channel.slug)
        windows.append({"start": start, "end": end})
    channel.clock_overlay_enabled = enabled
    channel.clock_windows = windows
    channel.save(update_fields=["clock_overlay_enabled", "clock_windows"])
    messages.success(request, "朝・夕の時計表示設定を保存しました。")
    return redirect("core:channel_settings", slug=channel.slug)


@staff_member_required
@require_POST
def set_broadcast_windows(request, slug: str) -> HttpResponse:
    """放送時間帯 (broadcast_windows) を保存。

    windows は 1 行 1 窓の「HH:MM-HH:MM」テキスト (JST)。終端は 24:00 も可 (翌日 00:00)。
    空欄なら 24 時間放送 (制限なし)。resolver は次の周期から休止スレートを発行する。
    """
    channel = get_object_or_404(Channel, slug=slug)
    windows: list[dict[str, str]] = []
    for line in (request.POST.get("broadcast_windows") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        start, _, end = line.partition("-")
        start, end = start.strip(), end.strip()
        if not (_HHMM_RE.match(start) and _HHMM_END_RE.match(end)):
            messages.error(
                request,
                f"時間帯は HH:MM-HH:MM 形式 (終端は 24:00 も可) で入力してください: {line!r}",
            )
            return redirect("core:channel_settings", slug=channel.slug)
        windows.append({"start": start, "end": end})
    channel.broadcast_windows = windows
    channel.save(update_fields=["broadcast_windows"])
    if windows:
        messages.success(
            request, "放送時間帯を保存しました。次の resolve 周期 (最大5分) から反映されます。"
        )
    else:
        messages.success(request, "放送時間帯を削除しました (24 時間放送に戻します)。")
    return redirect("core:channel_settings", slug=channel.slug)


# frontend/apps/studio/src/clockFonts.ts の FONTS と揃えること。
_VALID_FONT_FAMILIES = {
    # 日本語（ゴシック）
    "noto-sans-jp",
    "zen-kaku-gothic",
    "zen-kaku-antique",
    "biz-udgothic",
    "m-plus-1p",
    "murecho",
    "sawarabi-gothic",
    # 日本語（明朝）
    "noto-serif-jp",
    "shippori-mincho",
    "zen-old-mincho",
    "kaisei-decol",
    "sawarabi-mincho",
    # 日本語（丸・装飾）
    "zen-maru-gothic",
    "mplus-rounded",
    "kosugi-maru",
    "dela-gothic-one",
    "reggae-one",
    "rocknroll-one",
    "yuji-syuku",
    # 欧文（表示）
    "oswald",
    "bebas-neue",
    "anton",
    "saira-condensed",
    "teko",
    # 欧文（テック・SF）
    "orbitron",
    "michroma",
    "rajdhani",
    "chakra-petch",
    "exo-2",
    # 欧文（モノスペース）
    "share-tech-mono",
    "space-mono",
    "jetbrains-mono",
    "ibm-plex-mono",
}
_VALID_FONT_WEIGHTS = {400, 700, 900}
_VALID_TEXT_EFFECTS = {"none", "soft-shadow", "hard-outline", "glow"}
_VALID_STROKE_WIDTHS = {"none", "1", "2", "4"}
_VALID_BG_PRESETS = {"none", "dark-gradient", "dark-solid", "dark-pill", "frosted"}
_VALID_BOX_SHADOWS = {"none", "sm", "md", "lg"}
_VALID_BORDER_RADII = {"none", "sm", "md", "lg", "pill"}
_VALID_ANIMS = {"none", "fade", "slide-down", "zoom"}
_VALID_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right"}
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


@staff_member_required
@require_POST
def set_clock_style(request, slug: str) -> HttpResponse:
    """時計エディタ: スタイル + enabled + 時間帯を一括保存。studio SPA の postForm から呼ばれる。

    200 OK (text) = 成功、400 (text) = バリデーションエラー。redirect せず text を返す (SPA 向け)。
    """
    channel = get_object_or_404(Channel, slug=slug)
    p = request.POST.get

    def bad(msg: str) -> HttpResponse:
        return HttpResponse(msg, status=400)

    # --- フォント ---
    ff = p("font_family", "noto-sans-jp")
    if ff not in _VALID_FONT_FAMILIES:
        return bad("フォントファミリが不正です")

    try:
        ts = int(p("time_size", "54"))
        if not (36 <= ts <= 80):
            raise ValueError
    except ValueError:
        return bad("文字サイズは 36〜80 の整数で入力してください")

    try:
        fw = int(p("font_weight", "700"))
        if fw not in _VALID_FONT_WEIGHTS:
            raise ValueError
    except ValueError:
        return bad("フォントウェイトは 400 / 700 / 900 のいずれかです")

    # --- 色 ---
    tc = p("time_color", "#ffffff")
    dc = p("date_color", "#cfe3ff")
    if not _HEX_COLOR_RE.match(tc):
        return bad("時刻の文字色は #rrggbb 形式で入力してください")
    if not _HEX_COLOR_RE.match(dc):
        return bad("日付の文字色は #rrggbb 形式で入力してください")

    # --- エフェクト / 背景 / シャドウ / 角丸 ---
    te = p("text_effect", "soft-shadow")
    if te not in _VALID_TEXT_EFFECTS:
        return bad("テキストエフェクトが不正です")

    sw = p("stroke_width", "none")
    if sw not in _VALID_STROKE_WIDTHS:
        return bad("縁取り幅が不正です")
    sc = p("stroke_color", "#000000")
    if not _HEX_COLOR_RE.match(sc):
        return bad("縁取りの色は #rrggbb 形式で入力してください")

    bg = p("bg_preset", "dark-gradient")
    if bg not in _VALID_BG_PRESETS:
        return bad("背景プリセットが不正です")

    try:
        op = float(p("bg_opacity", "0.8"))
        if not (0.0 <= op <= 1.0):
            raise ValueError
    except ValueError:
        return bad("背景の不透明度は 0.0〜1.0 で入力してください")

    bs = p("box_shadow", "md")
    if bs not in _VALID_BOX_SHADOWS:
        return bad("ボックスシャドウが不正です")

    br = p("border_radius", "md")
    if br not in _VALID_BORDER_RADII:
        return bad("角丸が不正です")

    # --- アニメーション / 位置 ---
    anim = p("entrance_anim", "fade")
    if anim not in _VALID_ANIMS:
        return bad("アニメーションが不正です")

    pos = p("position", "top-left")
    if pos not in _VALID_POSITIONS:
        return bad("位置が不正です")

    # --- 表示オプション ---
    show_seconds = bool(request.POST.get("show_seconds"))
    show_date = bool(request.POST.get("show_date"))

    # --- enabled + 時間帯 ---
    enabled = bool(request.POST.get("clock_overlay_enabled"))
    windows: list[dict] = []
    for line in (p("clock_windows") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        start, _, end = line.partition("-")
        start, end = start.strip(), end.strip()
        if not (_HHMM_RE.match(start) and _HHMM_RE.match(end)):
            return bad(f"時間帯は HH:MM-HH:MM 形式で入力してください: {line!r}")
        windows.append({"start": start, "end": end})

    channel.clock_style = {
        "font_family": ff,
        "time_size": ts,
        "font_weight": fw,
        "time_color": tc,
        "date_color": dc,
        "text_effect": te,
        "stroke_width": sw,
        "stroke_color": sc,
        "bg_preset": bg,
        "bg_opacity": op,
        "box_shadow": bs,
        "border_radius": br,
        "entrance_anim": anim,
        "position": pos,
        "show_seconds": show_seconds,
        "show_date": show_date,
    }
    channel.clock_overlay_enabled = enabled
    channel.clock_windows = windows
    channel.save(update_fields=["clock_style", "clock_overlay_enabled", "clock_windows"])
    return HttpResponse("時計スタイルを保存しました", status=200)


# ---- チャンネル名 / slug 編集 (全ch一覧の専用画面) ----


@staff_member_required
def channel_list(request) -> HttpResponse:
    """全チャンネルの表示名 (name) と識別子 (slug) を一覧編集する専用画面。

    name は公開ページ/EPG/運用画面/JSON API に表示される名称 (例: 「総合」「教育」やスポンサー名)。
    slug は URL・送出 agent・YouTube/CF 連携の内部識別子で、変更は agent 再設定を伴う (テンプレ参照)。
    """
    channels = list(Channel.objects.order_by("slug"))
    return render(
        request,
        "admin_ui/channel_list.html",
        {"channels": channels, "active": "channels"},
    )


@staff_member_required
@require_POST
def channel_update(request, slug: str) -> HttpResponse:
    """1 チャンネルの name / slug を保存。slug は形式・一意性を検証し、エラーは messages で通知。

    slug を変更すると、その slug を識別子に使う送出ノードの agent が再設定まで gRPC で照合できなくなる。
    YouTube/Cloudflare の既存連携 ID は保持されるが、新規作成時のメタ名 (ICS-TV {slug} ingest) に影響する。
    現在 session で選択中の ch を rename した場合はナビが stale slug で 404 しないよう session も追従する。
    """
    channel = get_object_or_404(Channel, slug=slug)
    name = (request.POST.get("name") or "").strip()
    new_slug = (request.POST.get("slug") or "").strip()
    if not name:
        messages.error(request, "表示名は必須です。")
        return redirect("core:channel_list")
    if not new_slug:
        messages.error(request, "slug は必須です。")
        return redirect("core:channel_list")
    try:
        validate_slug(new_slug)
    except ValidationError:
        messages.error(
            request,
            f"slug の形式が不正です (半角英数・ハイフン・アンダースコアのみ): {new_slug!r}",
        )
        return redirect("core:channel_list")
    if Channel.objects.filter(slug=new_slug).exclude(pk=channel.pk).exists():
        messages.error(request, f"slug が既に他のチャンネルで使用されています: {new_slug}")
        return redirect("core:channel_list")
    # 公開フロント用の見た目メタ (#7 デザイン刷新)。略称は任意、識別色は #rrggbb 形式のみ受理。
    short = (request.POST.get("short") or "").strip()
    tint = (request.POST.get("tint") or "").strip()
    if tint and not re.fullmatch(r"#[0-9A-Fa-f]{6}", tint):
        messages.error(request, f"識別色は #rrggbb 形式で入力してください: {tint!r}")
        return redirect("core:channel_list")
    old_slug = channel.slug
    channel.name = name[:200]
    channel.slug = new_slug
    channel.short = short[:20]
    channel.tint = tint
    channel.save(update_fields=["name", "slug", "short", "tint"])
    if request.session.get("current_ch") == old_slug:
        request.session["current_ch"] = new_slug
    if old_slug != new_slug:
        messages.success(
            request,
            f"保存しました。slug を {old_slug} → {new_slug} に変更。"
            "送出ノードの agent 設定 (channel slug) も同じ値へ更新してください。",
        )
    else:
        messages.success(request, f"「{channel.name}」を保存しました。")
    return redirect("core:channel_list")


@staff_member_required
@require_POST
def set_cf_playback_url(request, slug: str) -> HttpResponse:
    """公開プレイヤー用の CF HLS 再生 URL を channel に保存 (#7 Phase 2 プレイヤー)。"""
    channel = get_object_or_404(Channel, slug=slug)
    channel.cf_playback_hls_url = (request.POST.get("cf_playback_hls_url") or "").strip() or None
    channel.save(update_fields=["cf_playback_hls_url"])
    return redirect("core:channel_settings", slug=channel.slug)


@staff_member_required
def youtube_oauth_start(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    try:
        flow = _build_flow(request)
    except OAuthNotConfiguredError as e:
        return HttpResponse(f"OAuth 未設定: {e}", status=503)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["youtube_oauth_state"] = state
    request.session["youtube_oauth_channel_slug"] = channel.slug
    # PKCE: authorization_url が生成した code_verifier を callback の token 交換まで保持する
    # (別リクエスト/別 Flow になるため。未保持だと "Missing code verifier" で token 交換が 500)。
    request.session["youtube_oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@staff_member_required
def youtube_oauth_callback(request) -> HttpResponse:
    expected_state = request.session.get("youtube_oauth_state")
    got_state = request.GET.get("state")
    if not expected_state or expected_state != got_state:
        return HttpResponse("OAuth state mismatch", status=400)
    slug = request.session.get("youtube_oauth_channel_slug")
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    try:
        flow = _build_flow(request, state=expected_state)
    except OAuthNotConfiguredError as e:
        return HttpResponse(f"OAuth 未設定: {e}", status=503)
    # PKCE: start で保存した code_verifier を復元してから token 交換する
    flow.code_verifier = request.session.get("youtube_oauth_code_verifier")
    flow.fetch_token(authorization_response=request.build_absolute_uri())
    creds = flow.credentials
    YoutubeCredential.objects.update_or_create(
        channel=channel,
        defaults={
            "client_id": os.environ["ICSTV_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["ICSTV_OAUTH_CLIENT_SECRET"],
            "refresh_token": creds.refresh_token or "",
            "access_token": creds.token,
            "token_expiry": creds.expiry,
        },
    )
    request.session.pop("youtube_oauth_state", None)
    request.session.pop("youtube_oauth_channel_slug", None)
    request.session.pop("youtube_oauth_code_verifier", None)
    return redirect("core:channel_settings", slug=slug)


# ---- 永続 liveStream 作成 (docs/youtube.md シーケンス1) ----


@staff_member_required
@require_POST
def create_persistent_stream_view(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    if channel.youtube_livestream_id:
        return HttpResponse(
            f"既に永続 liveStream が存在します (id={channel.youtube_livestream_id})。"
            "再作成は Django admin で channel を編集してリセットしてください。",
            status=409,
        )
    if not YoutubeCredential.objects.filter(channel=channel).exists():
        return HttpResponse(
            "YouTube OAuth 未連携。channel 設定画面の [接続] を先に完了してください。",
            status=412,
        )
    from youtube.api import create_persistent_stream

    try:
        create_persistent_stream(channel)
    except HttpError as e:
        return HttpResponse(f"YouTube API エラー: {e}", status=502)
    return redirect("core:channel_settings", slug=slug)


# ---- Cloudflare Live Input / Output (docs/overview.md 3.6) ----


@staff_member_required
@require_POST
def create_cf_live_input_view(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    if channel.cf_live_input_id:
        return HttpResponse(
            f"既に CF Live Input が存在します (id={channel.cf_live_input_id})。"
            "再作成は admin で channel をリセットしてください。",
            status=409,
        )
    from core import cloudflare_api

    try:
        cloudflare_api.create_live_input(channel)
    except cloudflare_api.CloudflareNotConfiguredError as e:
        return HttpResponse(f"Cloudflare 未設定: {e}", status=503)
    except httpx.HTTPStatusError as e:
        return HttpResponse(
            f"CF API エラー: status={e.response.status_code} body={e.response.text[:300]}",
            status=502,
        )
    return redirect("core:channel_settings", slug=slug)


@staff_member_required
@require_POST
def create_cf_live_output_view(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    if not channel.cf_live_input_id:
        return HttpResponse(
            "CF Live Input 未作成。先に [Live Input 作成] を実行してください。",
            status=412,
        )
    if not channel.youtube_stream_key:
        return HttpResponse(
            "YouTube 永続キーが未設定 (channel.youtube_stream_key)。"
            "YouTube 側で永続 liveStream を先に作成してください。",
            status=412,
        )
    # YouTube ingest URL は CF Live Output の宛先として固定 (rtmp://a.rtmp.youtube.com/live2)。
    target_url = os.environ.get("ICSTV_YOUTUBE_INGEST_URL", "rtmp://a.rtmp.youtube.com/live2")
    from core import cloudflare_api

    try:
        cloudflare_api.create_live_output(
            channel,
            target_url=target_url,
            stream_key=channel.youtube_stream_key,
        )
    except cloudflare_api.CloudflareNotConfiguredError as e:
        return HttpResponse(f"Cloudflare 未設定: {e}", status=503)
    except httpx.HTTPStatusError as e:
        return HttpResponse(
            f"CF API エラー: status={e.response.status_code} body={e.response.text[:300]}",
            status=502,
        )
    return redirect("core:channel_settings", slug=slug)


# ---- YouTube 配信メニュー (#23 で「設定」から分離した統合入口) ----


@staff_member_required
def youtube_console(request, slug: str) -> HttpResponse:
    """YouTube 配信メニューのランディング。rolling 枠 / 配信プリセット / 番組専用枠 (#23) の各
    ダッシュボードへの入口を集約する。OAuth/liveStream 等の接続設定は引き続き「設定」に残す。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    cred = YoutubeCredential.objects.filter(channel=channel).first()
    return render(
        request,
        "admin_ui/youtube_console.html",
        {
            "channel": channel,
            "active": "youtube",
            "connected": bool(cred and cred.refresh_token),
            "has_livestream": bool(channel.youtube_livestream_id),
            "has_second_stream": bool(channel.youtube_livestream_id_2),
            "preset_count": YoutubeBroadcastPreset.objects.count(),
            "dedicated_count": ProgramBroadcast.objects.filter(program__channel=channel).count(),
        },
    )


# ---- 枠ダッシュボード (docs/ui.md §B) ----


@staff_member_required
def slot_dashboard(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    # 直近 24 枠 (= rolling_hours=24 / slot_minutes=120 = 12 枠 + 過去分)
    slots = list(
        YoutubeSlot.objects.filter(channel=channel)
        .order_by("-window_start")
        .only(
            "id",
            "window_start",
            "window_end",
            "status",
            "broadcast_id",
            "title",
            "description",
            "manual",
            "error",
        )[:24]
    )
    return render(
        request,
        "admin_ui/slot_dashboard.html",
        {
            "channel": channel,
            "slots": slots,
            "active": "youtube",
        },
    )


# ---- 配信プリセット CRUD (#23) ----
# Studio の配信設定を再利用するプリセット。API 反映項目は ModelForm、API 不可分の
# 手動チェック項目 (manual_checklist) は default フラグをチェックボックスで編集する。


def _checklist_with_post_defaults(items: list[dict], post) -> list[dict]:
    """manual_checklist の各項目の default を POST のチェック状態で更新 (key/label は保持)。"""
    return [{**it, "default": bool(post.get(f"checklist_{it['key']}"))} for it in items]


@staff_member_required
def preset_list(request) -> HttpResponse:
    presets = list(YoutubeBroadcastPreset.objects.select_related("channel").all())
    return render(
        request, "admin_ui/broadcast_preset_list.html", {"presets": presets, "active": "youtube"}
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def preset_new(request) -> HttpResponse:
    if request.method == "POST":
        form = YoutubeBroadcastPresetForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.manual_checklist = _checklist_with_post_defaults(
                default_manual_checklist(), request.POST
            )
            obj.save()
            messages.success(request, f"プリセット「{obj.name}」を作成しました")
            return redirect("core:preset_edit", preset_id=obj.id)
    else:
        form = YoutubeBroadcastPresetForm()
    return render(
        request,
        "admin_ui/broadcast_preset_form.html",
        {"form": form, "checklist": default_manual_checklist(), "is_new": True},
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def preset_edit(request, preset_id: int) -> HttpResponse:
    preset = get_object_or_404(YoutubeBroadcastPreset, pk=preset_id)
    if request.method == "POST":
        form = YoutubeBroadcastPresetForm(request.POST, instance=preset)
        if form.is_valid():
            obj = form.save(commit=False)
            base = preset.manual_checklist or default_manual_checklist()
            obj.manual_checklist = _checklist_with_post_defaults(base, request.POST)
            obj.save()
            messages.success(request, "保存しました")
            return redirect("core:preset_edit", preset_id=preset.id)
    else:
        form = YoutubeBroadcastPresetForm(instance=preset)
    checklist = preset.manual_checklist or default_manual_checklist()
    return render(
        request,
        "admin_ui/broadcast_preset_form.html",
        {"form": form, "preset": preset, "checklist": checklist, "is_new": False},
    )


@staff_member_required
@require_POST
def preset_delete(request, preset_id: int) -> HttpResponse:
    preset = get_object_or_404(YoutubeBroadcastPreset, pk=preset_id)
    name = preset.name
    preset.delete()
    messages.success(request, f"プリセット「{name}」を削除しました")
    return redirect("core:preset_list")


_VALID_TRANSITIONS = {"testing", "live", "complete"}
_TRANSITION_STATUS = {
    "testing": "testing",
    "live": "live",
    "complete": "complete",
}


def apply_slot_transition(slot, target: str) -> tuple[bool, str, int]:
    """YoutubeSlot の配信遷移 (testing/live/complete) を実行する共有ロジック。

    studio (slot_transition_view) と 🔴放送コンソール (ops_views.op_slot_transition) の双方から
    呼ぶ。成功時 (ok, メッセージ, 200)、失敗時 (False, 理由, HTTP ステータス) を返し、
    呼び出し側が画面形式 (redirect / HTMX _ok) に変換する。
    """
    if target not in _VALID_TRANSITIONS:
        return False, f"invalid target={target!r}", 400
    if not slot.broadcast_id:
        return False, "broadcast_id 未設定 (枠が API 化されていない)", 412
    from youtube.api import transition_broadcast

    try:
        transition_broadcast(slot.channel, slot.broadcast_id, target)
    except HttpError as e:
        slot.status = "error"
        slot.error = str(e)[:1000]
        slot.save(update_fields=["status", "error"])
        return False, f"YouTube API エラー: {e}", 502
    slot.status = _TRANSITION_STATUS[target]
    slot.error = None
    slot.save(update_fields=["status", "error"])
    return True, f"配信枠を {target} に遷移しました", 200


@staff_member_required
@require_POST
def slot_transition_view(request, slot_id: int) -> HttpResponse:
    slot = get_object_or_404(YoutubeSlot.objects.select_related("channel"), pk=slot_id)
    ok, msg, status = apply_slot_transition(slot, request.POST.get("target", ""))
    if not ok:
        return HttpResponse(msg, status=status)
    return redirect("core:slot_dashboard", slug=slot.channel.slug)


@staff_member_required
@require_POST
def slot_meta_update_view(request, slot_id: int) -> HttpResponse:
    """枠のタイトル/キャプションを手動編集し、broadcast 化済みなら YouTube へ反映 (#7 枠メタ)。"""
    slot = get_object_or_404(YoutubeSlot.objects.select_related("channel"), pk=slot_id)
    title = (request.POST.get("title") or "").strip()[:300]
    description = (request.POST.get("description") or "").strip()
    if not title:
        return HttpResponse("タイトルは必須です", status=400)
    if slot.broadcast_id:
        from youtube.api import update_broadcast

        try:
            update_broadcast(
                slot.channel,
                slot.broadcast_id,
                title=title,
                scheduled_start=slot.window_start,
                description=description,
            )
        except HttpError as e:
            slot.status = "error"
            slot.error = str(e)[:1000]
            slot.save(update_fields=["status", "error"])
            return HttpResponse(f"YouTube API エラー: {e}", status=502)
    slot.title = title
    slot.description = description
    slot.manual = True
    slot.error = None
    slot.save(update_fields=["title", "description", "manual", "error"])
    return redirect("core:slot_dashboard", slug=slot.channel.slug)


@staff_member_required
@require_POST
def slot_apply_template_view(request, slot_id: int) -> HttpResponse:
    """枠のタイトル/説明を現在の YoutubeConfig テンプレートで再生成し、broadcast 化済みなら YouTube へ反映 (#7 枠メタ)。

    単枠版の resync_slot_meta。手動編集 (manual=True) を破棄してテンプレート内容へ戻すため、
    反映後は manual=False とし以後の自動 resync 追従下に戻す。
    """
    slot = get_object_or_404(
        YoutubeSlot.objects.select_related("channel"),
        pk=slot_id,
    )
    cfg = YoutubeConfig.objects.filter(channel=slot.channel).first()
    if cfg is None:
        return HttpResponse("youtube_config 未設定 (テンプレートがありません)", status=412)
    from youtube.tasks import _compose_slot_meta

    title, description = _compose_slot_meta(
        slot.channel,
        slot.window_start,
        slot.window_end,
        cfg.title_template,
        cfg.description_template,
    )
    if slot.broadcast_id:
        from youtube.api import update_broadcast

        try:
            update_broadcast(
                slot.channel,
                slot.broadcast_id,
                title=title,
                scheduled_start=slot.window_start,
                description=description,
            )
        except HttpError as e:
            slot.status = "error"
            slot.error = str(e)[:1000]
            slot.save(update_fields=["status", "error"])
            return HttpResponse(f"YouTube API エラー: {e}", status=502)
    slot.title = title
    slot.description = description
    slot.manual = False
    slot.error = None
    slot.save(update_fields=["title", "description", "manual", "error"])
    return redirect("core:slot_dashboard", slug=slot.channel.slug)


@staff_member_required
@require_POST
def slot_delete_view(request, slot_id: int) -> HttpResponse:
    slot = get_object_or_404(YoutubeSlot.objects.select_related("channel"), pk=slot_id)
    slug = slot.channel.slug
    if slot.broadcast_id:
        from youtube.api import delete_broadcast

        # 既に削除済み (404) / 権限不足など。DB は消すことで状態を復旧可能なので続行。
        with contextlib.suppress(HttpError):
            delete_broadcast(slot.channel, slot.broadcast_id)
    slot.delete()
    return redirect("core:slot_dashboard", slug=slug)


# ---- #23 番組専用枠 ダッシュボード + 手動操作 ----


@staff_member_required
def program_broadcast_dashboard(request, slug: str) -> HttpResponse:
    """チャンネルの番組専用枠 (#23) 一覧 + 手動操作 (今すぐ作成 / Go Live / 終了 / チェックリスト)。"""
    from scheduling.models import Program

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    now = timezone.now()

    broadcasts = list(
        ProgramBroadcast.objects.filter(program__channel=channel)
        .select_related("program", "preset")
        .order_by("-program__start_at")[:50]
    )
    have_ids = {pb.program_id for pb in broadcasts}
    # 表示用 view model (model へ動的属性を生やさない)。checklist 状態と watch URL を組み立てる。
    rows = []
    for pb in broadcasts:
        items = pb.preset.manual_checklist if pb.preset else default_manual_checklist()
        state = pb.checklist_state or {}
        rows.append(
            {
                "pb": pb,
                "watch": f"https://www.youtube.com/watch?v={pb.broadcast_id}"
                if pb.broadcast_id
                else "",
                "checklist": [
                    {
                        "key": it["key"],
                        "label": it["label"],
                        "checked": bool(state.get(it["key"], it.get("default"))),
                    }
                    for it in items
                ],
            }
        )

    # 専用枠フラグがあり、まだ枠が無い今後の番組 (手動作成候補)
    upcoming = [
        p
        for p in Program.objects.select_related(
            "series", "youtube_preset", "series__youtube_preset"
        )
        .filter(channel=channel, end_at__gt=now)
        .order_by("start_at")[:100]
        if p.wants_dedicated and p.id not in have_ids
    ][:30]

    return render(
        request,
        "admin_ui/program_broadcast_dashboard.html",
        {
            "channel": channel,
            "rows": rows,
            "upcoming": upcoming,
            "has_second_stream": bool(channel.youtube_livestream_id_2),
            "active": "youtube",
        },
    )


@staff_member_required
@require_POST
def program_broadcast_create_now(request, program_id: int) -> HttpResponse:
    """指定番組の専用枠を即時作成 (手動)。2 本目 liveStream が無ければ先にプロビジョンする。"""
    from scheduling.models import Program
    from youtube.api import create_dedicated_stream
    from youtube.tasks import create_program_broadcast

    program = get_object_or_404(
        Program.objects.select_related(
            "channel", "series", "youtube_preset", "series__youtube_preset"
        ),
        pk=program_id,
    )
    channel = program.channel
    preset = program.resolved_youtube_preset
    if preset is None:
        messages.error(request, "プリセット未設定です (番組またはシリーズで選択してください)")
        return redirect("core:program_broadcast_dashboard", slug=channel.slug)
    try:
        if not channel.youtube_livestream_id_2:
            create_dedicated_stream(channel)
        create_program_broadcast(channel, program, preset, manual=True)
    except (HttpError, ValueError) as e:
        messages.error(request, f"専用枠の作成に失敗: {e}")
        return redirect("core:program_broadcast_dashboard", slug=channel.slug)
    messages.success(request, f"専用枠を作成しました: {program.title}")
    return redirect("core:program_broadcast_dashboard", slug=channel.slug)


@staff_member_required
@require_POST
def program_broadcast_transition(request, pb_id: int) -> HttpResponse:
    """専用枠を手動で live / complete へ遷移 (beat を待たず Go Live / 終了)。"""
    from youtube.api import transition_broadcast

    pb = get_object_or_404(ProgramBroadcast.objects.select_related("program__channel"), pk=pb_id)
    channel = pb.program.channel
    target = request.POST.get("target", "")
    if target not in {"live", "complete"}:
        messages.error(request, f"不正な target: {target!r}")
        return redirect("core:program_broadcast_dashboard", slug=channel.slug)
    if not pb.broadcast_id:
        messages.error(request, "broadcast_id 未設定 (枠が未作成)")
        return redirect("core:program_broadcast_dashboard", slug=channel.slug)
    try:
        transition_broadcast(channel, pb.broadcast_id, target)
    except HttpError as e:
        pb.status = YtSlotStatus.ERROR
        pb.error = str(e)[:1000]
        pb.save(update_fields=["status", "error", "updated_at"])
        messages.error(request, f"YouTube API エラー: {e}")
        return redirect("core:program_broadcast_dashboard", slug=channel.slug)
    pb.status = YtSlotStatus.LIVE if target == "live" else YtSlotStatus.COMPLETE
    pb.error = None
    pb.save(update_fields=["status", "error", "updated_at"])
    messages.success(request, f"{target} に遷移しました")
    return redirect("core:program_broadcast_dashboard", slug=channel.slug)


@staff_member_required
@require_POST
def program_broadcast_checklist(request, pb_id: int) -> HttpResponse:
    """配信ごとの手動チェックリスト状態を保存 (API 不可分項目の運用記録)。"""
    pb = get_object_or_404(
        ProgramBroadcast.objects.select_related("program__channel", "preset"), pk=pb_id
    )
    items = pb.preset.manual_checklist if pb.preset else default_manual_checklist()
    pb.checklist_state = {
        it["key"]: bool(request.POST.get(f"checklist_{it['key']}")) for it in items
    }
    pb.save(update_fields=["checklist_state", "updated_at"])
    messages.success(request, "チェックリストを保存しました")
    return redirect("core:program_broadcast_dashboard", slug=pb.program.channel.slug)


@staff_member_required
@require_POST
def program_broadcast_delete(request, pb_id: int) -> HttpResponse:
    """専用枠を削除 (YouTube broadcast も best-effort で削除)。"""
    from youtube.api import delete_broadcast

    pb = get_object_or_404(ProgramBroadcast.objects.select_related("program__channel"), pk=pb_id)
    slug = pb.program.channel.slug
    if pb.broadcast_id:
        with contextlib.suppress(HttpError):
            delete_broadcast(pb.program.channel, pb.broadcast_id)
    pb.delete()
    messages.success(request, "専用枠を削除しました")
    return redirect("core:program_broadcast_dashboard", slug=slug)
