# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""運行ダッシュボード + 即時操作 API (#7 O-B / docs/operations.md O5)。staff 専用。

- ダッシュボード本体 `ops_dashboard` と、HTMX `hx-trigger="every 2s"` で各々独立に
  ポーリングされる 3 つの部分テンプレート (NOW PLAYING / ヘルス / AS-RUN)。
- 即時操作はすべて core.views.insert_immediate_event (即時 PlayoutEvent INSERT) に統一。
  緊急スレート (core.views.emergency_slate) と同型で、status=SCHEDULED→agent 配信→as-run で done。

NOW PLAYING の on-air 規約は core.views.home と同一: scheduled_at<=now の最新非 CANCELLED。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core import thumbnails
from core.consumers import broadcast_playout_update
from core.models import Channel, Notification
from core.views import fire_chime, insert_immediate_event, schedule_future_event
from medialib.models import Asset, CmBundle
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import LiveCue, Program, ProgramType
from scheduling.services import (
    CueNotPendingError,
    ExtendRejectedError,
    ShortenRejectedError,
    extend_program,
    fire_cm_now_cue,
    preview_extend,
    roll_vt_cue,
    shorten_program,
    skip_live_cue,
)
from youtube.models import YoutubeSlot

# agent_status.last_heartbeat_at がこれを超えて途絶したら offline 表示
# (playout.tasks.check_agent_liveness の OFFLINE_THRESHOLD_SEC と揃える)。
_OFFLINE_THRESHOLD = timedelta(seconds=90)
_NEXT_LIMIT = 8
_ASRUN_LIMIT = 20
_NOTIF_PANEL_LIMIT = 20
_NOTIF_CENTER_LIMIT = 100

BUMPER_LAYER = 20  # amcp_planner.LAYER_BUMPER — CM IN/OUT レイヤ


class EmptyCmBundleError(Exception):
    """空の CmBundle での発火 (op_cm_in の既存 412 と同じ意味を型で持たせる)。"""


# ---- 🔴 放送コンソール SPA シェル (リファクタ Phase 1 / ops.* 専用ホスト) ----


@staff_member_required
@ensure_csrf_cookie
def ops_app(request) -> HttpResponse:
    """ops.* 専用ホストの放送コンソール SPA シェル。studio (#Phase2d) と同型: #ops-root +
    ops.js を staff 限定で返すだけで、画面は React が描く (config.urls_ops が catch-all で割当)。
    ディープリンク (/ch1 等) も同じシェルへ。@ensure_csrf_cookie で P1.2 の非 GET 操作に備える。
    """
    return render(request, "ops/app.html")


def _notifications_q(channel: Channel) -> Q:
    """このチャンネル宛 + 全体宛 (channel=NULL) の通知。"""
    return Q(channel=channel) | Q(channel__isnull=True)


def _on_air(channel: Channel, now) -> PlayoutEvent | None:
    return (
        PlayoutEvent.objects.filter(channel=channel, scheduled_at__lte=now)
        .exclude(status=PlayoutStatus.CANCELLED)
        .select_related("program", "asset")
        .order_by("-scheduled_at")
        .first()
    )


def _now_playing_ctx(channel: Channel) -> dict:
    now = timezone.now()
    on_air = _on_air(channel, now)
    upcoming = list(
        PlayoutEvent.objects.filter(
            channel=channel, scheduled_at__gt=now, status=PlayoutStatus.SCHEDULED
        )
        .select_related("program")
        .order_by("scheduled_at")[:_NEXT_LIMIT]
    )
    # 押え/巻きの対象 = いま放送中の live 番組 (docs/operations.md O1)。
    live_program = (
        Program.objects.filter(
            channel=channel,
            type=ProgramType.LIVE,
            start_at__lte=now,
            end_at__gt=now,
        )
        .order_by("-start_at")
        .first()
    )
    return {
        "channel": channel,
        "on_air": on_air,
        "upcoming": upcoming,
        "now": now,
        "live_program": live_program,
    }


def _health_ctx(channel: Channel) -> dict:
    now = timezone.now()
    status = getattr(channel, "agent_status", None)
    online = bool(status and (now - status.last_heartbeat_at) <= _OFFLINE_THRESHOLD)
    current_slot = (
        YoutubeSlot.objects.filter(channel=channel, window_start__lte=now, window_end__gt=now)
        .order_by("-window_start")
        .first()
    )
    return {
        "channel": channel,
        "status": status,
        "online": online,
        "current_slot": current_slot,
        "now": now,
    }


def _asrun_ctx(channel: Channel) -> dict:
    rows = list(
        PlayoutEvent.objects.filter(channel=channel)
        .select_related("program")
        .order_by("-scheduled_at")[:_ASRUN_LIMIT]
    )
    return {"channel": channel, "rows": rows}


@staff_member_required
def ops_dashboard(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    # base.html ヘッダ (ch セレクタ / nav / 緊急SLATE) 用の共通 context。
    ctx = {
        "channel": channel,
        "channels": list(Channel.objects.filter(enabled=True).order_by("slug")),
        "current_ch_slug": channel.slug,
        "active": "ops",
        "bundles": list(CmBundle.objects.order_by("name")),
    }
    ctx.update(_now_playing_ctx(channel))
    ctx.update(_health_ctx(channel))
    ctx.update(_asrun_ctx(channel))
    ctx.update(_notifications_ctx(channel))
    return render(request, "ops/dashboard.html", ctx)


@staff_member_required
def panel_now_playing(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    return render(request, "ops/_now_playing.html", _now_playing_ctx(channel))


@staff_member_required
def panel_health(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    return render(request, "ops/_health.html", _health_ctx(channel))


@staff_member_required
def panel_asrun(request, slug: str) -> HttpResponse:
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    return render(request, "ops/_asrun.html", _asrun_ctx(channel))


# ---- 視聴計測ダッシュボード (#ADMIN-02) ----
def _concurrent_ctx() -> dict:
    """全 ch の同時視聴者数 (直近 PRESENCE_WINDOW 秒の在席)。"""
    from analytics import stats

    per_ch = stats.concurrent_by_channel(timezone.now())
    channels = list(Channel.objects.filter(enabled=True).order_by("slug"))
    rows = [{"channel": c, "concurrent": per_ch.get(c.id, 0)} for c in channels]
    return {"concurrent_rows": rows, "concurrent_total": sum(per_ch.values())}


@staff_member_required
def ops_analytics(request, slug: str) -> HttpResponse:
    """視聴計測 (#ADMIN-02)。同時接続数 (ch別、HTMX 自動更新) + 番組別ユニーク視聴トップ。"""
    from analytics import stats

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    ctx = {
        "channel": channel,
        "channels": list(Channel.objects.filter(enabled=True).order_by("slug")),
        "current_ch_slug": channel.slug,
        "active": "ops",
        "top_programs": stats.top_programs(15),
    }
    ctx.update(_concurrent_ctx())
    return render(request, "ops/analytics.html", ctx)


@staff_member_required
def panel_concurrent(request, slug: str) -> HttpResponse:
    get_object_or_404(Channel, slug=slug, enabled=True)
    return render(request, "ops/_concurrent.html", _concurrent_ctx())


# ---- 即時操作 API (staff + POST) ----


def _ok(message: str, trigger: str) -> HttpResponse:
    # HTMX 経由は部分応答 + HX-Trigger (トースト/パネル更新フック)。
    return HttpResponse(message, headers={"HX-Trigger": trigger})


@staff_member_required
@require_POST
def op_clear_slate(request, slug: str) -> HttpResponse:
    """スレート解除。clear_slate INSERT → planner が CLEAR {ch}-90 (本線 1-10 は保持)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    insert_immediate_event(
        channel, PlayoutAction.CLEAR_SLATE, {"interrupt": True}, discriminator="manual"
    )
    return _ok("スレート解除しました", "slateCleared")


@staff_member_required
@require_POST
def op_overlay(request, slug: str) -> HttpResponse:
    """手動グラフィック操作 (#18 §B): 任意レイヤ(1-89)へ CG/PLAY を即時発射。

    POST: layer(1-89) / op(show|update|hide|clear) / kind(graphic|video|text) /
          text(速報文言) or data(JSON) / template / clip / loop。
    本線(10)/スレート(90) 等は role 競合に注意 (UI 側で警告)。ハード禁止は 90 のみ。
    """
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    try:
        layer = int(request.POST.get("layer", "0"))
    except ValueError:
        layer = 0
    if not (1 <= layer <= 89):
        return _ok("レイヤは 1-89 を指定してください (90=スレート予約)", "overlayOp")
    op = request.POST.get("op", "show")
    params = {
        "overlay_layer": str(layer),
        "overlay_op": op,
        "overlay_kind": request.POST.get("kind", "text"),
        "interrupt": True,
    }
    if request.POST.get("template"):
        params["overlay_template"] = request.POST["template"]
    if request.POST.get("clip"):
        params["overlay_clip"] = request.POST["clip"]
    if request.POST.get("loop"):
        params["overlay_loop"] = "1"
    kind = params["overlay_kind"]
    text = (request.POST.get("text") or "").strip()
    if request.POST.get("data"):
        params["overlay_data"] = request.POST["data"]  # 生 JSON (複数組の上級用)
    elif kind == "graphic":
        # 画像(+文字, 位置)を 1 組の elements[] に組む (#18 §B.3)。
        url = ""
        if request.FILES.get("image"):
            try:
                url = thumbnails.store(request.FILES["image"])  # R2 → /t/<key>
            except thumbnails.ThumbnailError as e:
                return _ok(str(e), "overlayOp")
        elif request.POST.get("image_url"):
            url = request.POST["image_url"]
        # CEF(送出ノード)から取得するため相対 /t は公開ベース URL で絶対化
        if url.startswith("/") and settings.ICSTV_PUBLIC_BASE_URL:
            url = settings.ICSTV_PUBLIC_BASE_URL.rstrip("/") + url
        el: dict = {}
        if url:
            el["media"] = "image"
            el["url"] = url
        if text:
            el["text"] = text
        for f in ("x", "y", "w", "size"):
            v = request.POST.get(f)
            if v and v.lstrip("-").isdigit():
                el[f] = int(v)
        if request.POST.get("align"):
            el["align"] = request.POST["align"]
        if el:
            params["overlay_data"] = json.dumps({"elements": [el]}, ensure_ascii=False)
    elif text:  # kind=text (速報等)
        params["overlay_data"] = json.dumps({"text": text}, ensure_ascii=False)
    insert_immediate_event(
        channel, PlayoutAction.OVERLAY_OP, params, discriminator=f"overlay_{layer}_{op}"
    )
    # 手動速報パネルでチャイムが選ばれていれば show 時に層41で音を併発。値は ops_status の
    # chime_choices と同形式: "cat:<category>" (カテゴリ既定) | "snd:<id>" (ライブラリ直接)。
    chime = (request.POST.get("chime") or "").strip().lower()
    if op == "show" and chime and chime != "none":
        if chime.startswith("snd:") and chime[4:].isdigit():
            fire_chime([channel], sound_id=int(chime[4:]))
        else:
            fire_chime([channel], chime.removeprefix("cat:"))
    return _ok(f"レイヤ 1-{layer} に {op} を発射しました", "overlayOp")


@staff_member_required
@require_POST
def op_slot_transition(request, slug: str, slot_id: int) -> HttpResponse:
    """配信枠 (YT) の testing/live/complete 遷移 (リファクタ P1.3・放送コンソール用)。

    studio の slot_transition_view と同じ共有ロジック (admin_views.apply_slot_transition) を使うが、
    redirect ではなく HTMX 形式の _ok / エラー本文を返す (ops postForm が 200=成功で扱う)。
    slug でチャンネルを限定し、その配信枠だけを操作対象にする (放送ホストの越権防止)。
    """
    from core.admin_views import apply_slot_transition

    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    slot = get_object_or_404(
        YoutubeSlot.objects.select_related("channel"), pk=slot_id, channel=channel
    )
    ok, msg, status = apply_slot_transition(slot, request.POST.get("target", ""))
    if not ok:
        return HttpResponse(msg, status=status)
    return _ok(msg, "slotTransition")


@staff_member_required
@require_POST
def op_cut_live_return(request, slug: str) -> HttpResponse:
    """生の本線復帰 (手動)。直近の cut_live 設定を載せ直す cut_live + clear_slate の 2 段。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    last_live = (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CUT_LIVE)
        .order_by("-scheduled_at")
        .first()
    )
    params = dict(last_live.params) if last_live else {}
    params["interrupt"] = True
    ev = insert_immediate_event(
        channel, PlayoutAction.CUT_LIVE, params, discriminator="manual_return"
    )
    if last_live and last_live.live_source_id:
        ev.live_source_id = last_live.live_source_id
        ev.save(update_fields=["live_source"])
    insert_immediate_event(
        channel,
        PlayoutAction.CLEAR_SLATE,
        {"interrupt": True},
        discriminator="manual_return",
    )
    _hide_cm_bumper_now(channel)
    broadcast_playout_update(channel.slug)
    return _ok("生を本線へ復帰しました", "liveReturned")


@staff_member_required
@require_POST
def op_reload_main(request, slug: str) -> HttpResponse:
    """本線再ロード。現行オンエアイベントを再 INSERT。play_asset は経過分を頭出し補正。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    now = timezone.now()
    cur = _on_air(channel, now)
    if cur is None:
        return HttpResponse("現在のオンエアイベントがありません", status=412)
    params = dict(cur.params)
    params["interrupt"] = True
    if cur.asset_id and "asset_id" not in params:
        params["asset_id"] = str(cur.asset_id)
    if cur.action == PlayoutAction.PLAY_ASSET:
        # 無補正だとセグメント頭からの巻き戻りになる → 経過分を in_ms に足す。
        in_ms = int(params.get("in_ms", 0) or 0)
        elapsed_ms = max(0, int((now - cur.scheduled_at).total_seconds() * 1000))
        params["in_ms"] = str(in_ms + elapsed_ms)
    ev = insert_immediate_event(channel, cur.action, params, discriminator="reload")
    for fld in ("asset_id", "program_id", "live_source_id", "cm_bundle_id"):
        setattr(ev, fld, getattr(cur, fld))
    ev.save(update_fields=["asset", "program", "live_source", "cm_bundle"])
    return _ok("本線を再ロードしました", "mainReloaded")


@staff_member_required
@require_POST
def op_auto_return_toggle(request, slug: str) -> HttpResponse:
    """自動復帰トグル。agent_status.auto_return (operator intent) を更新。

    変更は SubscribeEvents が検出して AgentControl で agent へ push する (push 自体は別経路)。
    ON 復帰時はフラップ・サスペンドも解除 (agent も on_agent_control で解除する)。
    """
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    status = getattr(channel, "agent_status", None)
    if status is None:
        return HttpResponse("agent_status 未生成 (agent 未接続)", status=412)
    target = request.POST.get("auto_return")
    if target not in ("on", "off"):
        return HttpResponse("auto_return は on/off", status=400)
    status.auto_return = target == "on"
    fields = ["auto_return"]
    if status.auto_return:
        status.auto_return_suspended = False
        fields.append("auto_return_suspended")
    status.save(update_fields=fields)
    label = "ON" if status.auto_return else "OFF"
    return _ok(f"自動復帰を{label}にしました", "autoReturnToggled")


def _last_live(channel: Channel) -> PlayoutEvent | None:
    return (
        PlayoutEvent.objects.filter(channel=channel, action=PlayoutAction.CUT_LIVE)
        .order_by("-scheduled_at")
        .first()
    )


def _return_rtmp_params(last: PlayoutEvent | None) -> dict:
    """直近 cut_live から reel 末尾の自動本線復帰(_return_rtmp_step)向けパラメータを求める。
    通常運用の emit_live は agent 側で URL 組み立てさせるため rtmp_app/rtmp_key しか
    params に持たず rtmp_url は無い — その場合は return_rtmp_app/return_rtmp_key を渡し、
    agent 側に同じ組み立てをさせる(rtmp_url があれば従来通りそれを最優先)。
    """
    if not last:
        return {}
    if last.params.get("rtmp_url"):
        return {"return_rtmp_url": last.params["rtmp_url"]}
    if last.params.get("rtmp_app") and last.params.get("rtmp_key"):
        return {
            "return_rtmp_app": last.params["rtmp_app"],
            "return_rtmp_key": last.params["rtmp_key"],
        }
    return {}


def _fire_cm_clips(
    channel: Channel, clips: str, total_ms: int, *, bundle_id: int | None, discriminator: str
) -> PlayoutEvent:
    """clips 列を即時 INSERT + layer20 バンパー(reel 実尺後に自動 hide) + 任意チャイムを併発する
    (docs/timekeeper-live.md §5.3)。CmBundle 発火 (fire_cm_bundle) と grid 動的充填
    (fire_cm_dynamic) の共有実装。bundle_id は事前割付バンドルのときだけ (動的充填時は None)。"""
    params: dict[str, Any] = {"clips": clips, "interrupt": True}
    if bundle_id is not None:
        params["bundle_id"] = bundle_id
    params.update(_return_rtmp_params(_last_live(channel)))
    now = timezone.now()
    ev = insert_immediate_event(
        channel, PlayoutAction.PLAY_CM_BUNDLE, params, discriminator=discriminator
    )
    if bundle_id is not None:
        ev.cm_bundle_id = bundle_id
        ev.save(update_fields=["cm_bundle"])
    insert_immediate_event(
        channel,
        PlayoutAction.OVERLAY_OP,
        {
            "overlay_layer": str(BUMPER_LAYER),
            "overlay_op": "show",
            "overlay_kind": "graphic",
            "overlay_template": "bumper/cm-in",
            "overlay_data": "{}",
            "interrupt": True,
        },
        discriminator=f"cm_bumper_in:{discriminator}",
    )
    schedule_future_event(
        channel,
        PlayoutAction.OVERLAY_OP,
        {
            "overlay_layer": str(BUMPER_LAYER),
            "overlay_op": "hide",
            "overlay_kind": "graphic",
            "interrupt": True,
        },
        now + timedelta(milliseconds=total_ms),
        discriminator=f"cm_bumper_out:{discriminator}",
    )
    # §5.3「＋任意チャイム」: CM 入りに layer41 チャイムを併発する (category="cm_in")。
    # 音源 (ChannelChime cm_in / CHIME_CLIPS) が未設定なら fire_chime は no-op なので完全に任意。
    fire_chime([channel], "cm_in", duration_sec=3, caller_disc=f"cm_in:{discriminator}")
    return ev


def fire_cm_bundle(channel: Channel, bundle: CmBundle, *, discriminator: str) -> PlayoutEvent:
    """CmBundle reel を即時 INSERT + バンパー/チャイムを併発する。op_cm_in (手動bundle選択) と
    op_cm_now (cue発火・bundle 事前割付あり) の共有実装。"""
    items = list(bundle.items.select_related("cm_asset__asset").order_by("seq"))
    if not items:
        raise EmptyCmBundleError("空の CM バンドルです")
    clips = ",".join(f"cm/{it.cm_asset_id}:{it.cm_asset.asset.duration_ms or 0}" for it in items)
    total_ms = sum(it.cm_asset.asset.duration_ms or 0 for it in items)
    return _fire_cm_clips(
        channel, clips, total_ms, bundle_id=bundle.id, discriminator=discriminator
    )


def fire_cm_dynamic(
    channel: Channel, *, grid: str, target_ms: int, discriminator: str
) -> PlayoutEvent:
    """grid で在庫から CM を動的充填して発火する (タイムキープ Phase3 D3 §11)。
    生 CM cue が cm_bundle 未割付のとき op_cm_now が使う。在庫が無ければ EmptyCmBundleError。"""
    from scheduling.resolver import select_free_cms

    cms = select_free_cms(grid, target_ms, timezone.now())
    if not cms:
        raise EmptyCmBundleError("動的充填する CM 在庫がありません (grid/在庫を確認)")
    clips = ",".join(f"cm/{cm.asset_id}:{cm.asset.duration_ms or 0}" for cm in cms)
    total_ms = sum(cm.asset.duration_ms or 0 for cm in cms)
    return _fire_cm_clips(channel, clips, total_ms, bundle_id=None, discriminator=discriminator)


def fire_vt_asset(channel: Channel, asset: Asset, *, discriminator: str) -> PlayoutEvent:
    """LiveCue(kind=vt)の録画セグメントを即時INSERT。CMと違いバンパー無し(ユーザー確定判断)。
    Lバー/予告/自動グラフィックの継続性は、直近cut_liveのcg_* paramsをそのまま引き継いで確保する
    (emit_live再計算ではなく「直前の生の状態をそのまま延長」— 押え/巻き後のドリフトにも自動追従)。"""
    params = {
        "clip": f"asset/{asset.id}",
        "in_ms": 0,
        "out_ms": asset.duration_ms or 0,
        "interrupt": True,
    }
    if asset.r2_key:
        params["r2_key"] = asset.r2_key
    last = _last_live(channel)
    if last:
        params.update(_return_rtmp_params(last))
        for k, v in last.params.items():
            if k.startswith("cg_"):
                params[k] = v
    ev = insert_immediate_event(channel, PlayoutAction.PLAY_VT, params, discriminator=discriminator)
    ev.asset_id = asset.id
    ev.save(update_fields=["asset"])
    return ev


def _hide_cm_bumper_now(channel: Channel) -> None:
    """CM 明け手動戻し。予定済み自動 hide をキャンセルし、即時 hide を発火 (fire_breaking_telop の
    キャンセル→再作成トゥームストーン idiom と同じ)。
    """
    PlayoutEvent.objects.filter(
        channel=channel,
        action=PlayoutAction.OVERLAY_OP,
        params__overlay_op="hide",
        params__overlay_layer=str(BUMPER_LAYER),
        status=PlayoutStatus.SCHEDULED,
    ).update(status=PlayoutStatus.CANCELLED)
    insert_immediate_event(
        channel,
        PlayoutAction.OVERLAY_OP,
        {
            "overlay_layer": str(BUMPER_LAYER),
            "overlay_op": "hide",
            "overlay_kind": "graphic",
            "interrupt": True,
        },
        discriminator="cm_bumper_out_manual",
    )


@staff_member_required
@require_POST
def op_cm_in(request, slug: str) -> HttpResponse:
    """生→CM 割り込み。選択した CM バンドルを play_cm_bundle で即時 INSERT。

    reel 末尾の生復帰は agent (amcp_planner) が return_rtmp_url から積む。戻り先 rtmp は
    直近 cut_live の params から引く (docs/operations.md O5)。
    """
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    bundle_id = request.POST.get("bundle_id")
    if not bundle_id:
        return HttpResponse("bundle_id が必要です", status=400)
    bundle = get_object_or_404(CmBundle, pk=bundle_id)
    try:
        # discriminator は呼び出しごとに一意にする必要がある — schedule_future_event の
        # get_or_create キーは discriminator のみで決まり now を含まないため、固定文字列だと
        # 2回目以降の CM 入りが前回の (既に消化済みの) バンパー hide 行を再利用してしまい、
        # 新しい hide が一切スケジュールされなくなる。
        fire_cm_bundle(channel, bundle, discriminator=f"manual_cm_in:{timezone.now().isoformat()}")
    except EmptyCmBundleError as e:
        return HttpResponse(str(e), status=412)
    broadcast_playout_update(channel.slug)
    return _ok(f"CM IN ({bundle.name}) しました", "cmIn")


@staff_member_required
@require_POST
def op_cm_return(request, slug: str) -> HttpResponse:
    """CM→生 手動戻し。残尺自動の戻りは reel 末尾ステップが担うが、手動で即戻す経路。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    last = _last_live(channel)
    params = dict(last.params) if last else {}
    params["interrupt"] = True
    params.pop("return_rtmp_url", None)  # cut_live には不要
    ev = insert_immediate_event(
        channel, PlayoutAction.CUT_LIVE, params, discriminator="manual_cm_return"
    )
    if last and last.live_source_id:
        ev.live_source_id = last.live_source_id
        ev.save(update_fields=["live_source"])
    _hide_cm_bumper_now(channel)
    broadcast_playout_update(channel.slug)
    return _ok("CM から生へ戻しました", "cmReturn")


@staff_member_required
@require_POST
def op_cm_now(request, slug: str, cue_id: int) -> HttpResponse:
    """生キューシートの CM cue を今すぐ発火 (タイムキープ Phase 1・#25)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    get_object_or_404(
        LiveCue.objects.select_related("rundown__program__channel"),
        pk=cue_id,
        rundown__program__channel=channel,
    )
    try:
        fire_cm_now_cue(cue_id)
    except CueNotPendingError as e:
        return HttpResponse(str(e), status=409)
    except EmptyCmBundleError as e:
        return HttpResponse(str(e), status=412)
    broadcast_playout_update(channel.slug)
    return _ok("CM を発火しました", "cueFireCm")


@staff_member_required
@require_POST
def op_roll_vt(request, slug: str, cue_id: int) -> HttpResponse:
    """生キューシートの VT cue を今すぐ送出 (タイムキープ Phase 1・#25)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    get_object_or_404(
        LiveCue.objects.select_related("rundown__program__channel"),
        pk=cue_id,
        rundown__program__channel=channel,
    )
    try:
        roll_vt_cue(cue_id)
    except CueNotPendingError as e:
        return HttpResponse(str(e), status=409)
    broadcast_playout_update(channel.slug)
    return _ok("VT を送出しました", "cueRollVt")


@staff_member_required
@require_POST
def op_skip_cue(request, slug: str, cue_id: int) -> HttpResponse:
    """生キューシートの cue をスキップ (タイムキープ Phase 1・#25)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    get_object_or_404(
        LiveCue.objects.select_related("rundown__program__channel"),
        pk=cue_id,
        rundown__program__channel=channel,
    )
    try:
        skip_live_cue(cue_id)
    except CueNotPendingError as e:
        return HttpResponse(str(e), status=409)
    broadcast_playout_update(channel.slug)
    return _ok("cue をスキップしました", "cueSkipped")


# ---- 通知 3 層 (ステータス件数 / トースト相当パネル / 通知センター。#7 O-B Commit4) ----


def _notifications_ctx(channel: Channel) -> dict:
    unacked = list(
        Notification.objects.filter(
            _notifications_q(channel), acknowledged_at__isnull=True
        ).order_by("-created_at")[:_NOTIF_PANEL_LIMIT]
    )
    return {"channel": channel, "unacked": unacked, "unacked_count": len(unacked)}


@staff_member_required
def panel_notifications(request, slug: str) -> HttpResponse:
    """未確認通知パネル (HTMX ポーリング)。トースト/ステータスバーのデータ源。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    return render(request, "ops/_notifications.html", _notifications_ctx(channel))


@staff_member_required
def notification_center(request, slug: str) -> HttpResponse:
    """通知センター: 確認済み含む履歴一覧 (ui-operations.md §7)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    rows = list(
        Notification.objects.filter(_notifications_q(channel))
        .select_related("channel")
        .order_by("-created_at")[:_NOTIF_CENTER_LIMIT]
    )
    return render(
        request,
        "ops/notifications.html",
        {
            "channel": channel,
            "channels": list(Channel.objects.filter(enabled=True).order_by("slug")),
            "current_ch_slug": channel.slug,
            "active": "ops",
            "rows": rows,
        },
    )


@staff_member_required
@require_POST
def op_ack_notification(request, slug: str, pk: int) -> HttpResponse:
    """通知を既読化 (全体共有: acknowledged 1 つ。docs/operations.md 論点どおり個人別ではない)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    n = get_object_or_404(Notification, pk=pk)
    if n.channel_id not in (None, channel.id):
        return HttpResponse("別チャンネルの通知です", status=404)
    if n.acknowledged_at is None:
        n.acknowledged_at = timezone.now()
        n.acknowledged_by = request.user
        n.save(update_fields=["acknowledged_at", "acknowledged_by"])
    return _ok("既読にしました", "notifAcked")


@staff_member_required
@require_POST
def op_ack_all_notifications(request, slug: str) -> HttpResponse:
    """このチャンネル + 全体宛の未確認通知をまとめて既読化。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    Notification.objects.filter(_notifications_q(channel), acknowledged_at__isnull=True).update(
        acknowledged_at=timezone.now(), acknowledged_by=request.user
    )
    return _ok("すべて既読にしました", "notifAcked")


# ---- 押え (延長) / 巻き (早終い) (#7 O-C / docs/operations.md O1・O8) ----


def _delta_ms(request) -> int:
    raw = request.POST.get("delta_ms") or request.GET.get("delta_ms") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


@staff_member_required
def op_extend_preview(request, slug: str, program_id: int) -> HttpResponse:
    """押えの影響範囲プレビュー (確認ダイアログ用、読み取り専用)。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    get_object_or_404(Program, pk=program_id, channel=channel)
    delta = _delta_ms(request)
    preview = preview_extend(program_id, delta)
    return render(
        request,
        "ops/_extend_preview.html",
        {"channel": channel, "preview": preview, "delta_ms": delta},
    )


@staff_member_required
@require_POST
def op_extend(request, slug: str, program_id: int) -> HttpResponse:
    """押え (延長)。後続を窓内で繰り下げ。吸収不能/終了直前は 409。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    get_object_or_404(Program, pk=program_id, channel=channel)
    delta = _delta_ms(request)
    try:
        extend_program(program_id, delta)
    except ExtendRejectedError as e:
        return HttpResponse(str(e), status=409)
    broadcast_playout_update(channel.slug)
    return _ok(f"押え (+{delta // 60000}分) しました", "programExtended")


@staff_member_required
@require_POST
def op_shorten(request, slug: str, program_id: int) -> HttpResponse:
    """巻き (早終い)。live 限定。下限割れ/recorded は 409。"""
    channel = get_object_or_404(Channel, slug=slug, enabled=True)
    get_object_or_404(Program, pk=program_id, channel=channel)
    delta = _delta_ms(request)
    try:
        shorten_program(program_id, delta)
    except ShortenRejectedError as e:
        return HttpResponse(str(e), status=409)
    broadcast_playout_update(channel.slug)
    return _ok(f"巻き (-{delta // 60000}分) しました", "programShortened")
