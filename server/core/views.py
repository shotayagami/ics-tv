# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""運用ダッシュボード (now-playing 集約) + 公開番組表 + 緊急 SLATE 発火。

docs/ui.md 「運用画面」「公開番組表」参照。Phase 1 最小版。
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core import epg as epg_mod
from core import live_poster, now_playing
from core.models import Channel
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import Program

# 割り込み event の決定論的 idempotency_key 用 (resolver と同じ namespace)。
_NS_ICSTV = uuid.uuid5(uuid.NAMESPACE_DNS, "icstv.local")


def insert_immediate_event(
    channel: Channel,
    action: str,
    params: dict | None = None,
    *,
    discriminator: str = "",
) -> PlayoutEvent:
    """割り込み系の即時 PlayoutEvent INSERT (docs/operations.md O5)。

    scheduled_at=now / status=SCHEDULED で入れ、SubscribeEvents (O10 フィルタ) 経由で
    agent に配信させる。idempotency_key は (channel, action, discriminator, now) で決定論的に
    生成し二重発火を防ぐ (同一 now でも別 action/discriminator なら別キー)。
    """
    now = timezone.now()
    key = uuid.uuid5(_NS_ICSTV, f"{channel.id}:{action}:{discriminator}:{now.isoformat()}")
    return PlayoutEvent.objects.create(
        idempotency_key=key,
        channel=channel,
        scheduled_at=now,
        action=action,
        params=params or {},
        status=PlayoutStatus.SCHEDULED,
    )


def schedule_future_event(
    channel: Channel, action: str, params: dict, at, *, discriminator: str
) -> PlayoutEvent:
    """未来の1回限りイベントを get_or_create で冪等発行する (fire_chime/fire_breaking_telop/
    CM バンパー hide が共有する「今 show + 未来 hide」idiom の後半)。"""
    key = uuid.uuid5(_NS_ICSTV, f"{channel.id}:{discriminator}")
    ev, _ = PlayoutEvent.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "channel": channel,
            "scheduled_at": at,
            "action": action,
            "params": params,
            "status": PlayoutStatus.SCHEDULED,
        },
    )
    return ev


BREAKING_LAYER = 40  # LAYER_BREAKING (cg-layers.md §B.2 / amcp_planner.LAYER_BREAKING)
CHIME_LAYER = 41  # LAYER_CHIME (cg-layers.md / amcp_planner.LAYER_CHIME) 速報チャイム(音声)
HAZARD_MAP_LAYER = 38  # LAYER_HAZARD_MAP (amcp_planner) 津波沿岸/震度のフルスクリーン地図 (40 の下)
HAZARD_CORNER_LAYER = (
    37  # LAYER_HAZARD_CORNER (amcp_planner) 常時表示ミニマップ (地図+凡例のみ・画面右下)
)

# 地図種別 → (CasparCG テンプレート名, layer)。テンプレは deploy/playout-node/casparcg/template/map/*.html。
# tsunami=津波沿岸マップ / seismic=震度地図(全国) / seismic_zoom=震度地図(震源〜震度3エリアへ動的ズーム) /
# seismic_regional=震度地図(震源の緯度経度から8地方を自動判定して固定ズーム) / eew_panel=EEW横書き地図+地域 /
# eew_band=EEW縦書き地域名 (以上 layer38・排他)。data の形は seismic と同一 (テンプレ側で viewBox のみ変える)。
# tsunami_corner=津波ミニマップ (地図+凡例のみ・常時表示・layer37) は layer38 と独立に同時表示できる。
_HAZARD_MAP_SPECS: dict[str, tuple[str, int]] = {
    "tsunami": ("map/tsunami", HAZARD_MAP_LAYER),
    "seismic": ("map/seismic", HAZARD_MAP_LAYER),
    "seismic_zoom": ("map/seismic-zoom", HAZARD_MAP_LAYER),
    "seismic_regional": ("map/seismic-regional", HAZARD_MAP_LAYER),
    "eew_panel": ("map/eew-panel", HAZARD_MAP_LAYER),
    "eew_band": ("map/eew-band", HAZARD_MAP_LAYER),
    "tsunami_corner": ("map/tsunami-corner", HAZARD_CORNER_LAYER),
}
HAZARD_MAP_KINDS = frozenset(_HAZARD_MAP_SPECS)

# 速報カテゴリ → チャイムクリップ。送出ノードのメディアフォルダ基準の相対パス (拡張子なしで
# CasparCG が解決)。settings.CHIME_CLIPS で上書き可。未登録カテゴリは no-op (鳴らさない)。
_DEFAULT_CHIME_CLIPS = {
    "eew": "sfx/eew",  # 緊急地震速報 (警報) / 津波・Jアラート等の緊急
    "weather": "sfx/weather",  # 気象 (警報等)
    "general": "sfx/general",  # その他・手動
}


def _chime_clip(key: str | None) -> str | None:
    """カテゴリ key からチャイムクリップを解決 (settings/既定フォールバック)。未知/None は None。"""
    mapping = getattr(settings, "CHIME_CLIPS", None) or _DEFAULT_CHIME_CLIPS
    return mapping.get((key or "").strip().lower()) or None


def _resolve_chime_clip(channel, category: str | None, sound_id: int | None) -> str | None:
    """鳴らす音源 clip を解決。sound_id (発火時その場選択) を最優先し、無ければ channel の
    カテゴリ選択 (ChannelChime→ChimeSound)、さらに無ければ settings/既定 clip にフォールバック。

    clip は content-addressed (ライブラリ音源の差し替え/選択替えで名前が変わる) ため、ここで毎回
    引き直すことで最新の選択が即座に反映される。
    """
    from core.models import ChannelChime, ChimeSound

    if sound_id:
        sound = ChimeSound.objects.filter(pk=sound_id).first()
        return sound.clip if sound is not None else None
    cat = (category or "").strip().lower()
    if not cat:
        return None
    sel = ChannelChime.objects.select_related("sound").filter(channel=channel, category=cat).first()
    if sel is not None and sel.sound is not None:
        return sel.sound.clip
    return _chime_clip(cat)


def fire_chime(
    channels,
    category: str | None = None,
    *,
    sound_id: int | None = None,
    duration_sec: int = 6,
    caller_disc: str = "",
) -> None:
    """速報チャイム (layer41・音声) を指定チャンネル群へ鳴らす。

    kind=video の OVERLAY_OP で音声クリップを PLAY し (本線に触れずチャンネル音声へ重畳)、
    duration 秒後に STOP する未来イベントを併発して後始末する。sound_id (発火時その場選択) があれば
    その音源を、無ければ channel のカテゴリ選択 (ChannelChime) を使う。どちらも解決できなければ
    そのチャンネルは鳴らさない。速報テロップ (fire_breaking_telop) と手動 op (op_overlay) が呼ぶ。

    caller_disc は呼び出し元の disc 文字列 (テロップ本文+外側 now)。これを含めることで同一
    マイクロ秒に同カテゴリの chime を複数投入したとき chime_hide_key が衝突するのを防ぐ。
    """
    if not sound_id and not (category or "").strip():
        return
    dur = max(1, min(30, int(duration_sec or 6)))
    now = timezone.now()
    tag = f"snd{sound_id}" if sound_id else (category or "").strip().lower()
    for ch in channels:
        clip = _resolve_chime_clip(ch, category, sound_id)
        if not clip:
            continue
        disc = f"{tag}:{now.isoformat()}:{caller_disc[:40]}"
        insert_immediate_event(
            ch,
            PlayoutAction.OVERLAY_OP,
            {
                "overlay_layer": str(CHIME_LAYER),
                "overlay_op": "show",
                "overlay_kind": "video",  # PLAY 1-41 <clip> (音声付き producer)
                "overlay_clip": clip,
                "interrupt": True,
            },
            discriminator=f"chime_show:{disc}",
        )
        schedule_future_event(
            ch,
            PlayoutAction.OVERLAY_OP,
            {
                "overlay_layer": str(CHIME_LAYER),
                "overlay_op": "hide",  # STOP 1-41
                "overlay_kind": "video",
                "interrupt": True,
            },
            now + timedelta(seconds=dur),
            discriminator=f"chime_hide:{disc}",
        )


def fire_breaking_telop(
    channels,
    text: str,
    *,
    duration_sec: int = 90,
    op: str = "show",
    chime: str | None = None,
) -> list[str]:
    """速報テロップ (layer40) を指定チャンネル群へ発射し、発射した slug を返す。

    op=show は op_overlay (#18 §B) と同形の OVERLAY_OP を show し、duration 秒後に hide する未来
    イベントを併発して自動クリアする (agent は scheduled_at 到来で発火)。op=clear は即時 CLEAR。
    chime (eew|weather|general) を指定すると op=show 時に layer41 でチャイム音を併発する。
    地震速報サブシステム (別リポ icstv-earthquake) の内部エンドポイント + studio 手動送出が使う共通経路。
    """
    import json

    duration = max(10, min(600, int(duration_sec or 90)))
    now = timezone.now()
    fired: list[str] = []
    for ch in channels:
        if op == "clear":
            insert_immediate_event(
                ch,
                PlayoutAction.OVERLAY_OP,
                {
                    "overlay_layer": str(BREAKING_LAYER),
                    "overlay_op": "clear",
                    "overlay_kind": "text",
                    "interrupt": True,
                },
                discriminator="breaking_clear",
            )
        else:
            disc = f"{text[:40]}:{now.isoformat()}"
            insert_immediate_event(
                ch,
                PlayoutAction.OVERLAY_OP,
                {
                    "overlay_layer": str(BREAKING_LAYER),
                    "overlay_op": "show",
                    "overlay_kind": "text",
                    "overlay_data": json.dumps({"text": text}, ensure_ascii=False),
                    "interrupt": True,
                },
                discriminator=f"breaking_show:{disc}",
            )
            # 前の show が残した SCHEDULED hide を tombstone にして agent に取り消させる。
            # 新しい show が到着したので duration を現時刻から再計算した hide に差し替える。
            # 複数リクエストの並走では cancel と create の間に隙があるが、最悪両方の hide が
            # 残っても T+90s に両方発火して冪等クリアされるため送出上は安全。
            PlayoutEvent.objects.filter(
                channel=ch,
                action=PlayoutAction.OVERLAY_OP,
                params__overlay_op="hide",
                params__overlay_layer=str(BREAKING_LAYER),
                status=PlayoutStatus.SCHEDULED,
            ).update(status=PlayoutStatus.CANCELLED)
            schedule_future_event(
                ch,
                PlayoutAction.OVERLAY_OP,
                {
                    "overlay_layer": str(BREAKING_LAYER),
                    "overlay_op": "hide",
                    "overlay_kind": "text",
                    "interrupt": True,
                },
                now + timedelta(seconds=duration),
                discriminator=f"breaking_hide:{disc}",
            )
        fired.append(ch.slug)
    if op != "clear" and chime:
        # disc は show ブランチで定義される (op != "clear" の保証は外側の if で済み)。
        fire_chime(channels, chime, caller_disc=disc)
    return fired


def fire_hazard_map(
    channels,
    kind: str,
    data: dict | None = None,
    *,
    op: str = "show",
    duration_sec: int = 120,
) -> list[str]:
    """地図CG (kind=graphic) を発射し slug を返す。layer は kind ごとに固定 (_HAZARD_MAP_SPECS)。

    tsunami/seismic/eew_panel/eew_band は layer38 (フルスクリーン・排他) を共有するが、
    tsunami_corner は layer37 の常時表示ミニマップ (地図+凡例のみ・画面右下) で独立の layer を持ち、
    layer38 側の表示状態に関わらず同時に show/update/clear できる。

    fire_breaking_telop と同形。op=show は map/<kind>.html を ADD し duration 秒後に自動 hide する
    未来イベントを併発する。op=update は同一地図へ CG UPDATE で続報データを流し込み、auto-hide を
    現時刻から張り直す (続報が来る限り地図は出続け、途切れて duration 秒で自動的に消える)。
    op=clear は即時 CLEAR (警報解除時)。地図描画は CasparCG CEF テンプレ側 (点滅等は CSS/JS)。
    地震速報サブシステム (別リポ icstv-earthquake) の内部エンドポイントと studio 手動発火が使う。
    """
    import json

    spec = _HAZARD_MAP_SPECS.get((kind or "").strip().lower())
    if spec is None:
        return []
    template, layer = spec
    op = (op or "show").lower()
    duration = max(10, min(1800, int(duration_sec or 120)))
    now = timezone.now()
    payload = json.dumps(data or {}, ensure_ascii=False)
    fired: list[str] = []
    for ch in channels:
        if op == "clear":
            insert_immediate_event(
                ch,
                PlayoutAction.OVERLAY_OP,
                {
                    "overlay_layer": str(layer),
                    "overlay_op": "clear",
                    "overlay_kind": "graphic",
                    "interrupt": True,
                },
                discriminator=f"hazard_clear:{kind}",
            )
            _cancel_scheduled_hides(ch, layer)
            fired.append(ch.slug)
            continue

        disc = f"{kind}:{now.isoformat()}"
        if op == "update":
            insert_immediate_event(
                ch,
                PlayoutAction.OVERLAY_OP,
                {
                    "overlay_layer": str(layer),
                    "overlay_op": "update",
                    "overlay_kind": "graphic",
                    "overlay_data": payload,
                    "interrupt": True,
                },
                discriminator=f"hazard_update:{disc}",
            )
        else:  # show
            insert_immediate_event(
                ch,
                PlayoutAction.OVERLAY_OP,
                {
                    "overlay_layer": str(layer),
                    "overlay_op": "show",
                    "overlay_kind": "graphic",
                    "overlay_template": template,
                    "overlay_data": payload,
                    "interrupt": True,
                },
                discriminator=f"hazard_show:{disc}",
            )
        # show/update いずれも auto-hide を現時刻から張り直す (続報が続く限り地図は残る)。
        _cancel_scheduled_hides(ch, layer)
        hide_key = uuid.uuid5(_NS_ICSTV, f"{ch.id}:hazard_hide:{disc}")
        PlayoutEvent.objects.get_or_create(
            idempotency_key=hide_key,
            defaults={
                "channel": ch,
                "scheduled_at": now + timedelta(seconds=duration),
                "action": PlayoutAction.OVERLAY_OP,
                "params": {
                    "overlay_layer": str(layer),
                    "overlay_op": "hide",
                    "overlay_kind": "graphic",
                    "interrupt": True,
                },
                "status": PlayoutStatus.SCHEDULED,
            },
        )
        fired.append(ch.slug)
    return fired


def _cancel_scheduled_hides(channel, layer: int) -> None:
    """当該 layer の SCHEDULED な hide 未来イベントを tombstone にして agent に取消させる。"""
    PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_op="hide",
        params__overlay_layer=str(layer),
        status=PlayoutStatus.SCHEDULED,
    ).update(status=PlayoutStatus.CANCELLED)


def home(request):
    channels = list(Channel.objects.filter(enabled=True).order_by("slug"))
    current_slug = request.session.get("current_ch") or (channels[0].slug if channels else None)
    current = next((c for c in channels if c.slug == current_slug), None)
    now_playing = None
    next_event = None
    if current:
        now = timezone.now()
        now_playing = (
            PlayoutEvent.objects.filter(channel=current, scheduled_at__lte=now)
            .exclude(status=PlayoutStatus.CANCELLED)
            .order_by("-scheduled_at")
            .first()
        )
        next_event = (
            PlayoutEvent.objects.filter(
                channel=current, scheduled_at__gt=now, status=PlayoutStatus.SCHEDULED
            )
            .order_by("scheduled_at")
            .first()
        )
    return render(
        request,
        "core/home.html",
        {
            "channels": channels,
            "current_ch_slug": current_slug,
            "current_channel": current,
            "now_playing": now_playing,
            "next_event": next_event,
            "active": "home",
        },
    )


@staff_member_required
@require_POST
def emergency_slate(request, slug: str) -> HttpResponse:
    """緊急 SLATE 即時発火。POST → PlayoutEvent(action=play_slate, scheduled_at=now) を INSERT。

    既存の gRPC SubscribeEvents 経由で agent が受信し、planner が is_immediate=True で
    PLAY 1-90 "<slate>" LOOP を即発射する。resolver の cancel scope は PLAY_SLATE を
    除外するため、定期再解決でキャンセルされない。
    """
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    insert_immediate_event(
        channel,
        PlayoutAction.PLAY_SLATE,
        {"reason": "manual_emergency"},
        discriminator="manual",
    )
    if request.headers.get("HX-Request"):
        # HTMX 経由なら部分応答 + イベント発火 (ヘッダで通知パネル更新等にフック可)
        return HttpResponse("緊急SLATE 発火しました", headers={"HX-Trigger": "slateEngaged"})
    return redirect("home")


def thumb_serve(request, key: str) -> HttpResponse:
    """R2 に保存したサムネ画像をアプリ経由で公開配信 (#7 Phase 2)。

    thumbnails/ prefix のみ許可。公開ページでも使うため認証不要・長期キャッシュ。
    """
    from core import r2

    if not key.startswith("thumbnails/") or ".." in key:
        raise Http404()
    try:
        body, content_type = r2.get_object(key)
    except Exception as e:  # boto ClientError 等 (未存在/権限)
        raise Http404() from e
    resp = HttpResponse(body, content_type=content_type)
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


def _local_day_bounds(d):
    """ローカル日 d の [00:00, 翌00:00) を aware datetime で返す。"""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(d, time.min), tz)
    return start, start + timedelta(days=1)


def _parse_date(s, default):
    try:
        return datetime.strptime(s or "", "%Y-%m-%d").date()
    except ValueError:
        return default


def _operator_context() -> dict:
    """運営者の識別情報 (特定商取引法11条相当)。tokushoho/privacy/terms が共通で使う。"""
    return {
        "operator_legal_name": settings.ICSTV_OPERATOR_LEGAL_NAME,
        "operator_representative_name": settings.ICSTV_OPERATOR_REPRESENTATIVE_NAME,
        "operator_address": settings.ICSTV_OPERATOR_ADDRESS,
        "operator_phone": settings.ICSTV_OPERATOR_PHONE,
        "operator_hide_contact_details": settings.ICSTV_OPERATOR_HIDE_CONTACT_DETAILS,
        "operator_contact_email": settings.ICSTV_OPERATOR_CONTACT_EMAIL,
    }


def privacy_policy(request) -> HttpResponse:
    """プライバシーポリシー (公開・認証不要)。Google OAuth 検証で要求される。"""
    return render(
        request,
        "public/privacy.html",
        {"live_count": _live_count(timezone.now()), **_operator_context()},
    )


def terms_of_service(request) -> HttpResponse:
    """利用規約 (公開・認証不要)。"""
    return render(
        request,
        "public/terms.html",
        {"live_count": _live_count(timezone.now()), **_operator_context()},
    )


def tokushoho(request) -> HttpResponse:
    """特定商取引法に基づく表記 (公開・認証不要)。有料サブスクの法定表示。価格は Plan から。"""
    from subscriptions.models import Plan

    return render(
        request,
        "public/tokushoho.html",
        {
            "plans": Plan.objects.filter(is_active=True).order_by("rank"),
            "live_count": _live_count(timezone.now()),
            **_operator_context(),
        },
    )


def _home_columns(now):
    """全 enabled ch の (channel, 当日窓の編成 programs, current, upcoming)。

    トップのカード/ヒーロー/これからの番組 + /api/now で同じ解決を使うため共通化。
    """
    today = timezone.localdate()
    ds, de = _local_day_bounds(today)
    cols = []
    for ch in Channel.objects.filter(enabled=True).order_by("slug"):
        progs = _public_programs(ch, ds, de + timedelta(days=1))
        current = next((p for p in progs if p.start_at <= now < p.end_at), None)
        upcoming = next((p for p in progs if p.start_at > now), None)
        cols.append({"channel": ch, "programs": progs, "current": current, "upcoming": upcoming})
    return cols


def _home_cards(now, member=None):
    """公開トップ各 ch のカード (現在/次の編成 + ライブ判定/いま放送中タイトル)。

    member 省略時は匿名扱い (exposure_policy/ファンクラブ ティア軸のゲートは常に効く。
    現状の呼び出し元 now_json は hls_url/gate_reason を出力に含めないため実害は無いが、
    #27 Phase B レビューで「今後 now_json にこれらのフィールドを足すと再びゲートが
    素通りする」との指摘を受け、api.routers.home と同じく member を通せるようにした)。
    """
    return [
        now_playing.card(c["channel"], now, c["current"], c["upcoming"], member)
        for c in _home_columns(now)
    ]


def _live_count(now) -> int:
    """ナビ右の「LIVE n」バッジ用: いま online (実送出中) かつ放送休止中でない enabled ch 数。"""
    return sum(
        1
        for ch in Channel.objects.filter(enabled=True)
        if now_playing.is_online(ch, now) and not now_playing.is_broadcast_paused(ch, now)
    )


def _member_pinned_channel_ids(member) -> set[int]:
    """会員がピン留めしたチャンネル id 集合 (#EPG-04)。未ログインは空。先頭並べ替えに使う。"""
    if not member:
        return set()
    from members.models import ChannelFavorite

    return set(ChannelFavorite.objects.filter(member=member).values_list("channel_id", flat=True))


def _present_genres() -> list[str]:
    """公開番組 or 有効シリーズに付与済みのジャンルを Genre 定義順で返す (発見導線のチップ用)。"""
    from scheduling.models import Genre, Series

    present = set(
        Program.objects.filter(public_visible=True)
        .exclude(genre="")
        .values_list("genre", flat=True)
    ) | set(Series.objects.filter(is_active=True).exclude(genre="").values_list("genre", flat=True))
    return [g for g in Genre.values if g in present]


def public_home(request) -> HttpResponse:
    """視聴者向け公開トップ (#7 / #Phase2b)。

    ヒーロー + チャンネルカード (ライブ HLS プレビュー + /api/v1/home ポーリング) は React 島が
    描画する。Django は public_base シェル + 下部 SSR セクション (これからの番組 / おすすめ /
    人気の見逃し / 新着見逃し / ジャンルから探す) を返す。発見導線 (#DISC-01) は SSR のまま。
    """
    from analytics import stats as analytics_stats
    from members import recommend
    from scheduling import vod as vod_mod

    now = timezone.now()
    member = getattr(request, "member", None)
    cols = _home_columns(now)  # 下部「これからの番組」(cross_upcoming) に使う
    new_programs = list(vod_mod.available_vod_qs(now)[:8])
    new_vod = [{"program": p, "can_watch": vod_mod.can_watch(p, member)} for p in new_programs]
    # あなたへのおすすめ (#PERS-03): 会員の視聴履歴/マイリストのジャンルから未視聴の見逃しを薦める
    recommendations = [
        {"program": p, "can_watch": vod_mod.can_watch(p, member)}
        for p in recommend.recommended_vod(member)
    ]
    # 人気の見逃し (#DISC-01 ランキング・#ADMIN-02 視聴計測): 放送時のユニーク視聴者数順
    popular_programs = analytics_stats.popular_vod(now=now)
    # 在庫が薄いうちは「人気の見逃し」が「新着見逃し」と同じ顔ぶれになり水増しに見える (#4)。
    # 人気の全項目が新着に内包される間は人気節を畳む。在庫が増えれば自然に分離・再表示される。
    new_ids = {p.id for p in new_programs}
    if popular_programs and all(p.id in new_ids for p in popular_programs):
        popular_programs = []
    popular = [{"program": p, "can_watch": vod_mod.can_watch(p, member)} for p in popular_programs]
    return render(
        request,
        "public/home.html",
        {
            "upcoming": epg_mod.cross_upcoming(cols, now),
            "recommendations": recommendations,
            "popular": popular,
            "new_vod": new_vod,
            "browse_genres": _present_genres(),
            "live_count": _live_count(now),
            "now": now,
        },
    )


def now_json(request) -> JsonResponse:
    """公開トップ自動更新用 JSON (#7 ライブ表示)。各 ch のライブ状態/タイトル/次番組。

    poster はパスのみ返す。クライアントが ?ts=floor(now/10) を付けてバケットキャッシュさせる。
    """
    now = timezone.now()
    member = getattr(request, "member", None)
    cards = _home_cards(now, member)
    items = []
    for c in cards:
        upcoming = c["upcoming"]
        current = c["current"]
        items.append(
            {
                "slug": c["slug"],
                "name": c["name"],
                "short": c["short"],
                "tint": c["tint"],
                "online": c["online"],
                "live": c["live"],
                "title": c["title"],
                "nowtitle": c["nowtitle"],
                "is_rerun": c["is_rerun"],
                "genre": c["genre"],
                "genre_color": c["genre_color"],
                "poster": c["poster"],
                # 進行バー/カウントダウンはクライアントが epoch から毎秒算出する。
                "cur_start_ts": c["cur_start_ts"],
                "cur_end_ts": c["cur_end_ts"],
                "time": (
                    f"{timezone.localtime(current.start_at):%H:%M}"
                    f"–{timezone.localtime(current.end_at):%H:%M}"
                    if current
                    else None
                ),
                "next": (
                    {
                        "time": f"{timezone.localtime(upcoming.start_at):%H:%M}",
                        "title": upcoming.title,
                    }
                    if upcoming
                    else None
                ),
            }
        )
    resp = JsonResponse(
        {"now": now.isoformat(), "live_count": sum(1 for c in cards if c["live"]), "cards": items}
    )
    # CF/ブラウザに edge キャッシュさせない (全視聴者へ古い now-playing を配らない)。
    resp["Cache-Control"] = "no-store"
    return resp


@require_POST
def beat(request) -> JsonResponse:
    """ライブ視聴ハートビート (#ADMIN-02)。再生中のプレイヤーが ~30s 毎に POST。

    匿名 viewer は `icstv_vid` cookie で識別 (無ければ発行)。送出 channel の現在番組を
    サーバ側で解決し、在席 (ViewerPresence) を upsert + 番組ユニーク視聴 (ProgramView) を記録。
    CSRF は通常どおり保護 (公開ページが ensure_csrf_cookie で csrftoken を配り、JS が
    X-CSRFToken を送る)。返却は最小。
    """
    from analytics.models import ProgramView, ViewerPresence

    slug = (request.POST.get("ch") or "").strip()
    channel = Channel.objects.filter(slug=slug, enabled=True).first()
    if not channel:
        return JsonResponse({"ok": False}, status=400)  # 不明 ch
    now = timezone.now()
    had_cookie = bool(request.COOKIES.get("icstv_vid"))
    vid = request.COOKIES.get("icstv_vid") or uuid.uuid4().hex
    program = (
        Program.objects.filter(
            channel=channel, public_visible=True, start_at__lte=now, end_at__gt=now
        )
        .order_by("start_at")
        .first()
    )
    ViewerPresence.objects.update_or_create(
        viewer_id=vid,
        channel=channel,
        defaults={"last_seen": now, "program": program},
    )
    if program is not None:
        ProgramView.objects.get_or_create(program=program, viewer_id=vid)
    resp = JsonResponse({"ok": True})
    if not had_cookie:
        # 匿名集計用のランダム id (PII 無し)。1年有効・SameSite=Lax・本番は Secure・httponly。
        resp.set_cookie(
            "icstv_vid",
            vid,
            max_age=31_536_000,
            samesite="Lax",
            secure=not settings.DEBUG,
            httponly=True,
        )
    return resp


@csrf_exempt
@require_POST
def live_poster_ingest(request, slug: str) -> HttpResponse:
    """送出ノードからのライブ静止画 ingest (管理ホスト/LAN, token 認証, #7 ライブ表示)。

    認証は agent と同じ Channel.agent_token (暗号化フィールドのため slug 引き→復号比較)。
    body は JPEG。R2 (live/<slug>.jpg) へ上書き保存し公開トップが配信する。CSRF 免除 (非ブラウザ)。
    """
    token = request.headers.get("X-Agent-Token", "")
    channel = Channel.objects.filter(slug=slug, enabled=True).first()
    if (
        not channel
        or not channel.agent_token
        or not constant_time_compare(token, channel.agent_token)
    ):
        return HttpResponse(status=403)
    body = request.body
    if not body or len(body) > live_poster.MAX_BYTES:
        return HttpResponse(status=400)
    live_poster.put(slug, body)
    return HttpResponse(status=204)


def _poster_max_age(current, todays, now) -> int:
    """ライブ静止画を public キャッシュしてよい秒数 (#27 Phase B)。

    ゲート判定はリクエスト時点の状態でしか行えないため、既定の SERVE_TTL をそのまま返すと
    「公開扱いで CDN に載ったレスポンス」が、直後に始まるゲート対象番組の開始後も最大
    SERVE_TTL 秒そのまま配信されうる。現在番組の終わり (現在番組が無ければ次番組の始まり) までの
    残り秒数で上限を切ることでこの窓を閉じる。境界の先が同じく完全公開でも短く切るだけなので
    安全側に倒れる。算出できない場合は既定 TTL に戻すだけで、配信自体は妨げない。

    なお本クランプが閉じるのは編成どおりの境界のみで、放送中に管理画面から fc_required_level や
    exposure_policy を直接書き換えるアドホックな変更には追随しない (docs/fanclub.md §9)。
    """
    ttl = live_poster.SERVE_TTL
    try:
        if current is not None:
            boundary = current.end_at
        else:
            boundary = next((p.start_at for p in todays if p.start_at > now), None)
        if boundary is None:
            return ttl
        return max(0, min(ttl, int((boundary - now).total_seconds())))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return ttl


def live_poster_serve(request, slug: str) -> HttpResponse:
    """ライブ静止画の公開配信 (#7 ライブ表示)。短期キャッシュ。未保存なら 404 (テンプレ側で fallback)。

    静止画は実際の送出映像を ~10s おきに撮った実フレームのため (core.live_poster)、これも
    「共有 Live Input」の露出経路の一つ (#27 Phase B)。exposure_policy/ファンクラブ ティア軸で
    現在番組がゲートされているときは同じ理由で 404 にする (公開/非公開を区別しない一様なレスポンスに
    することで、この画像そのものが「今はゲート中」の追加情報を漏らさないようにする)。

    ゲート対象のときは Cache-Control を private/no-store にする。既定の public キャッシュのままだと
    CDN/共有キャッシュが「たまたま最初に来た1人」の可否をそのまま他の視聴者へ配ってしまい、
    per-viewer のゲートが意味を失うため (完全公開の番組は従来どおり public キャッシュを維持し、
    高頻度アクセスでも origin/R2 read を増やさない)。

    公開扱いで返すときの max-age は次の編成境界までにクランプする (_poster_max_age)。判定は
    リクエスト時点の状態でしか行えないため、境界の直前に CDN へ載ったレスポンスがそのまま
    ゲート対象番組の開始後も配信される窓 (最大 SERVE_TTL 秒) が空くのを防ぐ。
    """
    from scheduling.exposure_gate import can_watch_live, requires_site_member_gate
    from scheduling.models import ExposurePolicy

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    now = timezone.now()
    today = timezone.localdate()
    ts, te = _local_day_bounds(today)
    todays = _public_programs(channel, ts, te + timedelta(days=1))
    current = next((p for p in todays if p.start_at <= now < p.end_at), None)
    member = getattr(request, "member", None)
    gated = not can_watch_live(current, member)
    if gated:
        # per-viewer の判定結果 (404) を共有キャッシュに残さない。.jpg は CDN 既定でキャッシュ
        # 対象になりやすく、無指定だとゲート解除後もこの 404 が居座って全視聴者に配られる。
        gated_resp = HttpResponseNotFound()
        gated_resp["Cache-Control"] = "private, no-store"
        return gated_resp
    try:
        body = live_poster.get(slug)
    except Exception as e:  # boto ClientError 等 (未存在/権限)
        raise Http404() from e
    resp = HttpResponse(body, content_type="image/jpeg")
    policy = current.resolved_exposure_policy if current is not None else ExposurePolicy.PUBLIC
    per_viewer_sensitive = current is not None and (
        requires_site_member_gate(policy) or current.fc_required_level is not None
    )
    if per_viewer_sensitive:
        resp["Cache-Control"] = "private, no-store"
    else:
        resp["Cache-Control"] = f"public, max-age={_poster_max_age(current, todays, now)}"
    return resp


def public_guide(request) -> HttpResponse:
    """番組表 (#7 / #Phase2 React 島化)。グリッドは島が /api/v1/guide から描画する。

    Django は public_base シェル + 初期日付 (data-base-date) のみ返す。日付ナビ/自動スクロールは島側。
    """
    today = timezone.localdate()
    base = _parse_date(request.GET.get("date"), today)
    resp = render(
        request,
        "public/guide.html",
        {"base_date": base, "live_count": _live_count(timezone.now())},
    )
    # data-base-date に当日を焼くため、CDN/bfcache でシェルが古い日付のまま配信されると
    # 島が過去日で開いてしまう。常に新鮮なシェルを返す (#2)。
    resp["Cache-Control"] = "no-store"
    return resp


def public_guide_week(request) -> HttpResponse:
    """週間番組表 (チャンネル毎・1ch × 7日)。?ch=<slug> でチャンネル、?date= で週の起点 (既定=今日)。

    現 /guide/ は全ch×単日グリッド。こちらは 1ch を縦時間軸 × 7 日列で俯瞰する別ビュー。
    """
    now = timezone.now()
    today = timezone.localdate()
    channels = list(Channel.objects.filter(enabled=True).order_by("slug"))
    start = _parse_date(request.GET.get("date"), today)
    if not channels:
        return render(
            request,
            "public/week.html",
            {"channels": [], "channel": None, "live_count": 0, "now": now},
        )
    slug = request.GET.get("ch")
    channel = next((c for c in channels if c.slug == slug), channels[0])

    from scheduling.resolver import project_filler_segments

    days = [start + timedelta(days=i) for i in range(7)]
    ws, _ = _local_day_bounds(days[0])
    _, we = _local_day_bounds(days[-1])
    progs = _public_programs(channel, ws, we)  # 週窓 (7日) に掛かる public program を一括取得
    by_day: dict = {d: [] for d in days}
    rerun_by_day: dict = {}
    for d in days:
        ds, de = _local_day_bounds(d)
        by_day[d] = [p for p in progs if p.end_at > ds and p.start_at < de]  # 日跨ぎは両日へ
        # 編成に無いフィラー帯を再放送 (rerun_eligible 素材) として日毎に投影 (表示専用)。
        rerun_by_day[d] = project_filler_segments(channel, ds, de)

    resp = render(
        request,
        "public/week.html",
        {
            "channels": channels,
            "channel": channel,
            "week": epg_mod.build_week_epg(channel, days, by_day, now, rerun_by_day=rerun_by_day),
            "start_date": start,
            "end_date": days[-1],
            "today": today,
            "prev_date": start - timedelta(days=7),
            "next_date": start + timedelta(days=7),
            "live_count": _live_count(now),
            "now": now,
        },
    )
    # 当日ハイライト/グリッドを SSR で焼くため、キャッシュ配信されると古い週が出る (#2)。
    resp["Cache-Control"] = "no-store"
    return resp


def _public_programs(channel, start, end):
    return list(
        Program.objects.filter(
            channel=channel,
            public_visible=True,
            end_at__gt=start,
            start_at__lt=end,
        )
        .select_related("asset", "series")
        .order_by("start_at")
    )


@ensure_csrf_cookie  # 視聴ハートビート(#ADMIN-02)が X-CSRFToken を送れるよう csrftoken を必ず配る
def public_epg(request, slug: str) -> HttpResponse:
    """視聴者向けプレイヤー画面 (#7 / #Phase1 React 島化, 認証不要)。

    本体 (プレイヤー/番組情報/本日の編成/コメント) は React 島 (/static/web/player) が
    ninja API (/api/v1/channels/...) から描画する。Django は public_base シェル + SEO/OG メタ
    だけを返す。OG/タイトルは現在番組を server-render するため current のみ解決する。
    """
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    now = timezone.now()
    current = (
        Program.objects.filter(
            channel=channel, public_visible=True, start_at__lte=now, end_at__gt=now
        )
        .order_by("start_at")
        .first()
    )
    return render(
        request,
        "public/epg.html",
        {"channel": channel, "current": current, "live_count": _live_count(now), "now": now},
    )


@ensure_csrf_cookie  # 操作バー島の お気に入り/リマインド トグルが X-CSRFToken を送れるよう csrftoken を配る
def public_program_detail(request, program_id: int) -> HttpResponse:
    """番組詳細 (#EPG-01)。あらすじ/出演者/ジャンル/放送時刻 + 状態別CTA (ライブ/見逃し/放送予定)。

    EPG・検索からの着地。public_visible のみ。放送済みで VOD 公開なら見逃し再生へ誘導。
    """
    from scheduling import vod as vod_mod

    now = timezone.now()
    program = get_object_or_404(
        Program.objects.select_related("channel", "series", "asset"),
        pk=program_id,
        public_visible=True,
    )
    if program.start_at <= now < program.end_at:
        state = "live"
    elif program.end_at <= now:
        state = "aired"
    else:
        state = "upcoming"
    vod_available = vod_mod.available_vod_qs(now).filter(pk=program.pk).exists()
    # live 番組の見逃し = YouTube アーカイブ (#VOD-01 live)。放送済み live のみ解決。
    yt_archive_url = ""
    if state == "aired" and program.type == "live":
        from youtube.archive import archive_watch_url

        yt_archive_url = archive_watch_url(program)
    upcoming_airings = []
    if program.series_id:
        upcoming_airings = list(
            Program.objects.filter(
                series_id=program.series_id, public_visible=True, start_at__gt=now
            )
            .select_related("channel")
            .order_by("start_at")[:5]
        )
    og_image = request.build_absolute_uri(program.thumb_url) if program.thumb_url else ""
    # #Phase2c: 操作バー (状態別CTA + お気に入り/リマインド/共有) は React 島が
    # /api/v1/program/{id} から描画。本文 (タイトル/あらすじ/出演者/今後の放送) は SEO のため SSR。
    return render(
        request,
        "public/program.html",
        {
            "program": program,
            "state": state,
            "vod_available": vod_available,
            "yt_archive_url": yt_archive_url,
            "upcoming_airings": upcoming_airings,
            "og_image": og_image,
            "now": now,
        },
    )


def public_search(request) -> HttpResponse:
    """番組・チャンネル横断検索 (#DISC-02 / #Phase2c)。

    結果は React 島 (/static/web/discover) が /api/v1/search からライブ検索で描画する。
    Django は public_base シェル + <title> 用の q のみ返す。
    """
    return render(
        request,
        "public/search.html",
        {"q": (request.GET.get("q") or "").strip(), "now": timezone.now()},
    )


def public_browse(request) -> HttpResponse:
    """ジャンル別ブラウズ (#DISC-01 / #Phase2c)。

    ジャンルタブ/結果カードは React 島 (/static/web/discover) が /api/v1/browse から描画する。
    Django は public_base シェル + <title>/OG 用の genre のみ返す。
    """
    return render(
        request,
        "public/browse.html",
        {"genre": (request.GET.get("genre") or "").strip(), "now": timezone.now()},
    )


def public_vod_list(request) -> HttpResponse:
    """見逃し配信 一覧 (#VOD-01 / #Phase2c)。

    録画 + YouTube アーカイブのカードは React 島 (/static/web/discover) が /api/v1/vod から描画する。
    Django は public_base シェルのみ返す。
    """
    return render(request, "public/vod.html", {"now": timezone.now()})


def public_vod_detail(request, program_id: int) -> HttpResponse:
    """見逃し再生ページ (#VOD-01)。可視性ゲートを通れば R2 署名URLを埋めて再生。

    available_vod_qs に無い番組 (非公開/期間外/未放送/未正規化) は 404。視聴不可は導線へ誘導。
    署名URLを含むため no-store (CF/共有キャッシュ禁止)。
    """
    from urllib.parse import urlencode

    from scheduling import vod as vod_mod

    now = timezone.now()
    program = get_object_or_404(vod_mod.available_vod_qs(now), pk=program_id)
    member = getattr(request, "member", None)
    if not vod_mod.can_watch(program, member, now):
        reason = vod_mod.gate_reason(program, member, now)
        if reason == "login":
            return redirect(f"/members/login/?{urlencode({'next': f'/vod/{program_id}/'})}")
        if reason == "verify":
            return redirect("/members/verify-required/")
        if reason == "subscribe":
            return redirect("/subscriptions/")
        if reason == "fc_join":
            # ファンクラブ未加入。番組ページの参加パネルへ誘導 (専用ランディングは新設しない)。
            series = program.series
            series_url = series.public_url if series is not None else "/"
            return redirect(series_url)
        if reason == "fc_unavailable":
            # creator 未紐付/停止中、または有料ティア要求 (Phase A は加入導線なし=準備中)。
            resp = render(
                request,
                "public/fanclub_unavailable.html",
                {"program": program, "now": now},
                status=403,
            )
            resp["Cache-Control"] = "no-store"
            return resp
        if reason == "age":
            # 年齢制限により視聴不可 (#BILL-02)。署名URLは渡さず告知 interstitial を 403 で返す。
            resp = render(
                request, "public/age_restricted.html", {"program": program, "now": now}, status=403
            )
            resp["Cache-Control"] = "no-store"
            return resp
        raise Http404()
    og_image = request.build_absolute_uri(program.thumb_url) if program.thumb_url else ""
    # 続きから再生 (#PERS-02): 視聴済み位置を復元 (完了済みは頭から)
    from members.models import WatchHistory

    member = getattr(request, "member", None)
    resume_ms = 0
    if member:
        wh = WatchHistory.objects.filter(member=member, program=program).first()
        if wh and not wh.completed:
            resume_ms = wh.position_ms
    resp = render(
        request,
        "public/vod_detail.html",
        {
            "program": program,
            "play_url": vod_mod.playback_url(program),
            "og_image": og_image,
            "resume_ms": resume_ms,
            "now": now,
        },
    )
    resp["Cache-Control"] = "no-store"
    return resp


def public_vod_captions(request, program_id: int) -> HttpResponse:
    """VOD 素材の字幕 VTT を同一オリジンで配信 (#PLAYER-04・決定#24)。

    <track> の CORS を避けるため R2 の VTT をアプリ経由 (thumb_serve と同じ手口) で返す。
    本体 (public_vod_detail) と同じ視聴ゲート (can_watch) を通す (gated 番組の文字起こし流出防止)。
    存在を漏らさないため不可視/未生成は一律 404。
    """
    from core import r2
    from scheduling import vod as vod_mod

    now = timezone.now()
    program = get_object_or_404(vod_mod.available_vod_qs(now), pk=program_id)
    member = getattr(request, "member", None)
    if not vod_mod.can_watch(program, member, now):
        raise Http404()
    asset = program.playback_asset
    if asset is None or not asset.has_caption:
        raise Http404()
    try:
        body, _ct = r2.get_object(asset.caption_r2_key)
    except Exception as e:  # boto ClientError 等 (未存在/権限)
        raise Http404() from e
    resp = HttpResponse(body, content_type="text/vtt; charset=utf-8")
    resp["Cache-Control"] = "no-store"
    return resp


def _render_series_detail(request, series, now) -> HttpResponse:
    """Series インスタンスを受け取ってシリーズ詳細テンプレートを描画する共通ヘルパ。"""
    from fanclub import services as fc_services
    from scheduling import vod as vod_mod
    from scheduling.models import AudienceForm, Program, SeriesPost

    programs = (
        Program.objects.filter(series=series, public_visible=True)
        .select_related("channel", "asset")
        .order_by("-start_at")[:60]
    )
    vod_ids = set(vod_mod.available_vod_qs(now).values_list("id", flat=True))
    member = getattr(request, "member", None)
    creator = fc_services.creator_for_series(series)
    my_level = fc_services.member_level(member, creator) if creator else None
    # 受付中の有料ティアと、その会員に対して取れる操作 (#27 §3.1)。/fc/<slug>/ と共用ヘルパ。
    my_membership, fc_tiers = fc_services.tier_rows(member, creator)
    posts = list(SeriesPost.objects.filter(series=series, is_published=True))
    for post in posts:
        # #27: series 単位で解決済みの creator/member を使い回すので投稿数によらず追加クエリなし。
        post.locked = post.fc_required_level is not None and not fc_services.can_view_level(
            post.fc_required_level, member, creator
        )
    audience_forms = list(AudienceForm.objects.filter(series=series, enabled=True))
    og_image = request.build_absolute_uri(series.thumbnail_url) if series.thumbnail_url else ""
    return render(
        request,
        "public/series_detail.html",
        {
            "series": series,
            "programs": programs,
            "vod_ids": vod_ids,
            "posts": posts,
            "audience_forms": audience_forms,
            "og_image": og_image,
            "now": now,
            "creator": creator,
            "my_level": my_level,
            "my_membership": my_membership,
            "fc_tiers": fc_tiers,
        },
    )


def public_series_detail(request, series_id: int) -> HttpResponse:
    """シリーズ詳細 / 番組紹介ページ (ID URL)。"""
    from scheduling.models import Series

    now = timezone.now()
    series = get_object_or_404(Series, pk=series_id, is_active=True)
    # slug がある場合は canonical URL へ 301 リダイレクト
    if series.slug:
        from django.http import HttpResponsePermanentRedirect

        return HttpResponsePermanentRedirect(f"/series/{series.slug}/")
    return _render_series_detail(request, series, now)


def public_series_detail_by_slug(request, slug: str) -> HttpResponse:
    """シリーズ詳細 / 番組紹介ページ (slug URL)。"""
    from scheduling.models import Series

    now = timezone.now()
    series = get_object_or_404(Series, slug=slug, is_active=True)
    return _render_series_detail(request, series, now)


def public_series_post(request, series_id: int, post_id: int) -> HttpResponse:
    """シリーズ投稿詳細ページ(記事/キャンペーン)。ファンクラブ限定はロック時に本文を渡さない。"""
    from fanclub import services as fc_services
    from scheduling.models import Series, SeriesPost

    now = timezone.now()
    series = get_object_or_404(Series, pk=series_id, is_active=True)
    post = get_object_or_404(SeriesPost, pk=post_id, series=series, is_published=True)
    member = getattr(request, "member", None)
    creator = fc_services.creator_for_series(series)
    reason = fc_services.fc_gate_reason(post.fc_required_level, member, creator)
    if reason:
        # ロック中は本文/メディアをテンプレへ一切渡さない (OG description も固定文言、漏えい防止)。
        return render(
            request,
            "public/series_post_locked.html",
            {
                "series": series,
                "post": post,
                "creator": creator,
                "gate_reason": reason,
                "meta_description": "ファンクラブ会員限定の投稿です。",
                "og_image": "",
                "now": now,
            },
        )
    og_image = (
        request.build_absolute_uri(post.media_url)
        if post.media_url
        else (request.build_absolute_uri(series.thumbnail_url) if series.thumbnail_url else "")
    )
    return render(
        request,
        "public/series_post.html",
        {
            "series": series,
            "post": post,
            "og_image": og_image,
            "now": now,
        },
    )
