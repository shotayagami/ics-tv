# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PlayoutAgent gRPC servicer (Phase 1)。

Phase 1 設計:
- SubscribeEvents: 5s polling で sync_seq > last_known_seq の event を流す。
  - 後続フェーズで PostgreSQL LISTEN/NOTIFY + trigger に置換し低レイテンシ化する余地を残す。
- ReportResult: idempotency_key で playout_event を更新。
- Heartbeat: ログ記録のみ (専用テーブルは Phase 1 後半 or Zabbix 連携時に追加)。
- 認証: 各 RPC 冒頭で Channel.agent_token と Bearer を都度照合。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from datetime import UTC

import grpc
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from google.protobuf.timestamp_pb2 import Timestamp
from icstv.v1 import playout_pb2, playout_pb2_grpc

from core import notify as notify_mod
from core.consumers import broadcast_playout_update
from core.models import Channel, NotificationSeverity
from medialib.models import CmCreative, FillerItem
from playout.models import AgentStatus, PlayoutAction, PlayoutEvent, PlayoutStatus
from scheduling.models import Program

logger = logging.getLogger(__name__)


_ACTION_DB_TO_PROTO = {
    "play_asset": playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
    "play_cm": playout_pb2.PLAYOUT_ACTION_PLAY_CM,
    "play_cm_bundle": playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
    "cut_live": playout_pb2.PLAYOUT_ACTION_CUT_LIVE,
    "play_filler": playout_pb2.PLAYOUT_ACTION_PLAY_FILLER,
    "play_slate": playout_pb2.PLAYOUT_ACTION_PLAY_SLATE,
    "yt_transition": playout_pb2.PLAYOUT_ACTION_YT_TRANSITION,
    "clear_slate": playout_pb2.PLAYOUT_ACTION_CLEAR_SLATE,
    "overlay_op": playout_pb2.PLAYOUT_ACTION_OVERLAY_OP,  # #18 §B
    "play_vt": playout_pb2.PLAYOUT_ACTION_PLAY_VT,
    "yt_mirror_filler": playout_pb2.PLAYOUT_ACTION_YT_MIRROR_FILLER,  # #27 exposure_policy
    "yt_mirror_route": playout_pb2.PLAYOUT_ACTION_YT_MIRROR_ROUTE,
}

_RESULT_PROTO_TO_DB = {
    playout_pb2.RESULT_STATUS_DONE: PlayoutStatus.DONE,
    playout_pb2.RESULT_STATUS_SKIPPED: PlayoutStatus.SKIPPED,
    playout_pb2.RESULT_STATUS_FAILED: PlayoutStatus.FAILED,
}


def _fill_payload(msg: playout_pb2.PlayoutEvent, action: str, p: dict) -> None:
    """内部 params (jsonb) を action 別 typed payload に翻訳する (gRPC 契約の型付け)。

    agent はこの payload を一次情報として dispatch する。clip/尺/区間など構造化データは
    ここで payload に載せ、params には補助 (media_url / cg_sponsor 等) のみ残す。
    """
    r2_key = p.get("r2_key") or ""
    if action == PlayoutAction.PLAY_ASSET:
        msg.play_asset.clip = p.get("clip", "")
        msg.play_asset.in_ms = int(p.get("in_ms", 0) or 0)
        msg.play_asset.out_ms = int(p.get("out_ms", 0) or 0)
        msg.play_asset.r2_key = r2_key
    elif action == PlayoutAction.PLAY_CM:
        msg.play_cm.clip = p.get("clip", "")
        msg.play_cm.r2_key = r2_key
    elif action == PlayoutAction.PLAY_CM_BUNDLE:
        # params["clips"] = "cm/2001:15000,cm/2002:20000" を BundleItem へ展開
        for tok in (p.get("clips") or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            clip, _, dur = tok.partition(":")
            msg.play_cm_bundle.items.add(clip=clip.strip(), duration_ms=int(dur or 0))
    elif action == PlayoutAction.CUT_LIVE:
        # 完全 URL があれば載せる。無ければ params の rtmp_app/key から agent がローカル
        # MediaMTX (127.0.0.1) で組み立てる (host は送出ノード固有のため server は持たない)。
        if p.get("rtmp_url"):
            msg.cut_live.rtmp_url = p["rtmp_url"]
    elif action == PlayoutAction.PLAY_FILLER:
        msg.play_filler.clip = p.get("clip", "")
        msg.play_filler.loop = bool(p.get("loop"))
        msg.play_filler.in_ms = int(p.get("in_ms", 0) or 0)
        msg.play_filler.out_ms = int(p.get("out_ms", 0) or 0)
    elif action == PlayoutAction.PLAY_VT:
        msg.play_vt.clip = p.get("clip", "")
        msg.play_vt.in_ms = int(p.get("in_ms", 0) or 0)
        msg.play_vt.out_ms = int(p.get("out_ms", 0) or 0)
        msg.play_vt.r2_key = r2_key


_MEDIA_URL_TTL = 518400  # presigned GET URL の TTL (6 日)。agent は最大 48h 先まで保持しうる。


def _build_prefetch_manifest(channel: Channel) -> list[dict]:
    """チャンネルの standing メディア (filler playlist 全件 + slate) を presigned 付きで返す。

    agent はこれを受けて全 clip を pre-cache+pin する (LRU 退避されない常駐)。filler ローテーション
    の巡回先が必ず手元に在る状態を作り、未キャッシュ→LOADBG 404→slate 固着 を根絶する (Phase2)。
    CM/番組(キューシート) は予定先行で各 event が r2_key を持ち通常の prefetch 経路に乗るため、
    この standing manifest には含めない (時間で変動するものを常駐させない)。
    """
    from core import r2

    items: list[dict] = []
    if channel.default_filler_id is not None:
        fitems = (
            FillerItem.objects.filter(filler_playlist_id=channel.default_filler_id)
            .select_related("asset")
            .order_by("seq")
        )
        for it in fitems:
            if not it.asset.r2_key:
                continue
            try:
                url = r2.presign_get(it.asset.r2_key, expires=_MEDIA_URL_TTL)
            except Exception:
                logger.warning("manifest presign 失敗 filler asset=%s", it.asset_id)
                continue
            items.append({"clip": f"filler/{it.asset_id}", "url": url})
    slate = channel.slate_asset
    if slate is not None and slate.r2_key:
        try:
            url = r2.presign_get(slate.r2_key, expires=_MEDIA_URL_TTL)
            items.append({"clip": f"slate/{slate.id}", "url": url, "slate": True})
        except Exception:
            logger.warning("manifest presign 失敗 slate asset=%s", slate.id)
    # exposure_policy(#27): YTミラー用の案内フィラー2種。slate と同型 (単発Asset、常駐pin)。
    site_only_filler = channel.site_only_filler
    if site_only_filler is not None and site_only_filler.r2_key:
        try:
            url = r2.presign_get(site_only_filler.r2_key, expires=_MEDIA_URL_TTL)
            items.append(
                {
                    "clip": f"site_only_filler/{site_only_filler.id}",
                    "url": url,
                    "site_only_filler": True,
                }
            )
        except Exception:
            logger.warning("manifest presign 失敗 site_only_filler asset=%s", site_only_filler.id)
    members_filler = channel.members_filler
    if members_filler is not None and members_filler.r2_key:
        try:
            url = r2.presign_get(members_filler.r2_key, expires=_MEDIA_URL_TTL)
            items.append(
                {
                    "clip": f"members_filler/{members_filler.id}",
                    "url": url,
                    "members_filler": True,
                }
            )
        except Exception:
            logger.warning("manifest presign 失敗 members_filler asset=%s", members_filler.id)
    # 速報チャイム音源ライブラリ (ChimeSound) 全体を standing メディアとして pin+DL させる。音源は
    # 小容量なので全件常駐させ、カテゴリ選択の切替や手動速報の発火時選択を無DLで即時反映できる。
    from core.models import ChimeSound

    for cs in ChimeSound.objects.all():
        if not cs.r2_key:
            continue
        try:
            url = r2.presign_get(cs.r2_key, expires=_MEDIA_URL_TTL)
            items.append({"clip": cs.clip, "url": url})
        except Exception:
            logger.warning("manifest presign 失敗 chime sound=%s", cs.id)
    return items


def _event_to_proto(
    ev: PlayoutEvent, prefetch_manifest: list[dict] | None = None
) -> playout_pb2.PlayoutEvent:
    """Django PlayoutEvent → proto。action 別 typed payload (oneof) を埋め、params は補助のみ。

    prefetch_manifest は呼び出し側 (sync 文脈) が構築して渡す。async ストリーム文脈から
    _build_prefetch_manifest を直接呼ぶと DB クエリが SynchronousOnlyOperation で落ちるため。
    """
    ts = Timestamp()
    ts.FromDatetime(ev.scheduled_at)
    msg = playout_pb2.PlayoutEvent(
        idempotency_key=str(ev.idempotency_key),
        sync_seq=ev.sync_seq or 0,
        channel_slug=ev.channel.slug,
        scheduled_at=ts,
        action=_ACTION_DB_TO_PROTO.get(ev.action, playout_pb2.PLAYOUT_ACTION_UNSPECIFIED),
        # リゾルバが再解決で消した event は CANCELLED に落ちる → agent に tombstone 伝搬。
        tombstone=(ev.status == PlayoutStatus.CANCELLED),
    )
    p = ev.params or {}
    # 互換 + 補助のため params も載せる (media_url / cg_sponsor / interrupt 等)。
    # agent は構造化データを payload から読み、params はフォールバック/補助に限る。
    msg.params.update({k: str(v) for k, v in p.items()})
    _fill_payload(msg, ev.action, p)
    # prefetch (overview 3.2): mezzanine R2 キーがあれば presigned GET URL を付与する。
    # agent は WAN 越しに 48h 先まで保持しうるため TTL を長め (6 日) に取る。配信のたび再付与され、
    # 失敗しても本線送出には影響しない (agent は media_url 無しなら prefetch をスキップ)。
    r2_key = p.get("r2_key")
    if r2_key:
        try:
            from core import r2

            msg.params["media_url"] = r2.presign_get(r2_key, expires=_MEDIA_URL_TTL)
        except Exception:
            logger.warning("media_url presign 失敗 key=%s (prefetch スキップ)", r2_key)
    # filler イベントに standing メディア manifest (filler 全件 + slate) を同梱する。filler は
    # 周期発行されるため agent は定期的に最新の manifest を受け取り pre-cache+pin できる (Phase2)。
    if (
        ev.action in (PlayoutAction.PLAY_FILLER, PlayoutAction.YT_MIRROR_FILLER)
        and prefetch_manifest
    ):
        msg.params["prefetch_manifest"] = json.dumps(prefetch_manifest, separators=(",", ":"))
    return msg


async def _verify_token(context: grpc.aio.ServicerContext, channel_slug: str) -> None:
    metadata = dict(context.invocation_metadata())
    auth = metadata.get("authorization", "")
    if not auth.startswith("Bearer "):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Bearer token required")
    token = auth.removeprefix("Bearer ").strip()

    def _check() -> bool:
        # agent_token は暗号化 (非決定的) のため WHERE で照合できない。slug で 1 行に絞り、
        # 復号済の値 (model read で透過復号) と定数時間比較する (#3)。
        ch = Channel.objects.filter(slug=channel_slug, enabled=True).only("agent_token").first()
        if ch is None or not ch.agent_token or not token:
            return False
        return hmac.compare_digest(ch.agent_token, token)

    if not await sync_to_async(_check)():
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid channel/token")


# agent へ配信してよい status。SCHEDULED=未実行(実行対象) / CANCELLED=tombstone(キューから除去)。
# それ以外 (EXECUTING/DONE/FAILED/SKIPPED) は配信しない。とりわけ server が status=done で
# INSERT する割り込み記録 (feed 断自動退避などの ReportInterrupt 由来) をエコー配信すると、
# agent 側に該当行が無いため新規 SCHEDULED 扱いで即時再実行され、実行済みのスレートを再点火
# する等の二重実行が起きる (docs/operations.md 決定 O10)。
_SUBSCRIBE_STATUSES = (PlayoutStatus.SCHEDULED, PlayoutStatus.CANCELLED)


@sync_to_async
def _fetch_events_after(
    channel_slug: str, cursor: int, limit: int
) -> tuple[list[PlayoutEvent], list[dict] | None]:
    events = list(
        PlayoutEvent.objects.select_related(
            "channel",
            "channel__slate_asset",
            "channel__site_only_filler",
            "channel__members_filler",
        )
        .filter(
            channel__slug=channel_slug,
            sync_seq__gt=cursor,
            status__in=_SUBSCRIBE_STATUSES,
        )
        .order_by("sync_seq")[:limit]
    )
    # standing manifest は filler イベントがある時だけ sync 文脈で構築 (async では DB 不可)。
    # ストリームは単一チャンネルなので events[0].channel を共用する。
    manifest = None
    if any(e.action in (PlayoutAction.PLAY_FILLER, PlayoutAction.YT_MIRROR_FILLER) for e in events):
        manifest = _build_prefetch_manifest(events[0].channel)
    return events, manifest


@sync_to_async
def _fetch_auto_return(channel_slug: str) -> bool | None:
    """operator トグルの意図 (agent_status.auto_return)。行が無ければ None。

    SubscribeEvents が値の変化を検出して AgentControl を push する (docs/operations.md O4/O5)。
    """
    return (
        AgentStatus.objects.filter(channel__slug=channel_slug)
        .values_list("auto_return", flat=True)
        .first()
    )


def apply_result(idempotency_key: str, status: str, actual_at, note: str) -> bool:
    """as-run 実績を playout_event に反映。PLAY_CM の初回 DONE で aired_count を増分。

    aired_count は「実送出が確定したとき」(= agent の as-run 返送時) に増やす
    (docs/scheduler.md、resolver.fill_break の均等ローテ/出稿上限が依存)。
    agent の outbox 再送で同じ idempotency_key が複数回届きうるため、DONE への
    初回遷移 (旧 status != DONE) に限って増分し二重計上を防ぐ。row は SELECT FOR
    UPDATE で確保し、並行 ReportResult でも増分を直列化する。

    タイムキープ Phase2 §3/D5: 初回 DONE かつ params["live_cue_id"] を持つ本体イベント
    (PLAY_CM_BUNDLE/PLAY_VT) は、対応する LiveCue (state=PENDING 限定) を AIRED 化する
    (自動発火が実際に完了したことを義務台帳に反映する)。

    CANCELLED (tombstone) は終端状態として扱う: 手動発火/スキップでキャンセルした直後の
    イベントを、agent 側で既に in-flight だった古い as-run 報告 (outbox 再送・
    SubscribeEvents のポーリング遅延中に実行済みだった等) が後から DONE 等で上書きして
    しまうと、二重発火の実害を隠蔽したまま記録だけ「正常完了」に見せかけてしまう
    (2026-07-04 レビュー指摘)。CANCELLED からの遷移は行わず、受信自体は accepted 扱いにする
    (ReportResult 側の「idempotency_key not found」警告と区別するため True を返す)。
    """
    with transaction.atomic():
        ev = (
            PlayoutEvent.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
        )
        if ev is None:
            return False
        if ev.status == PlayoutStatus.CANCELLED:
            logger.info(
                "ReportResult: idempotency_key=%s は CANCELLED 済みのため as-run 報告を無視",
                idempotency_key,
            )
            return True
        first_done = status == PlayoutStatus.DONE and ev.status != PlayoutStatus.DONE
        ev.status = status
        ev.actual_at = actual_at
        ev.note = note
        ev.save(update_fields=["status", "actual_at", "note"])

        if first_done and ev.action == PlayoutAction.PLAY_CM and ev.asset_id is not None:
            # CmCreative は asset と 1:1 (pk == asset_id)。CM 以外/CmCreative 無しは 0 件で no-op。
            CmCreative.objects.filter(pk=ev.asset_id).update(aired_count=F("aired_count") + 1)

        if first_done:
            # タイムキープ Phase2 §3/D5: auto_fire で予約発行された event (params["live_cue_id"]
            # を持つ) が実際に DONE (as-run) になったら、対応する LiveCue を aired 化して
            # 義務台帳 (残CM/残VT) の整合性を保つ。手動発火/skip とは scheduling.services 側で
            # 排他しているが、万一の競合に備え state=PENDING の cue のみを対象にし、既に
            # AIRED/SKIPPED (= 手動側が先に処理済み) の cue を上書きしない。
            # バンパー (OVERLAY_OP) も同じ live_cue_id を params に持つが、cue の aired 化を
            # 担うのは本体イベント (PLAY_CM_BUNDLE/PLAY_VT) のみなので action で絞る。
            live_cue_id = (ev.params or {}).get("live_cue_id")
            if live_cue_id and ev.action in (
                PlayoutAction.PLAY_CM_BUNDLE,
                PlayoutAction.PLAY_VT,
            ):
                from scheduling.models import LiveCue, LiveCueState  # 遅延import (循環回避)

                updated = LiveCue.objects.filter(pk=live_cue_id, state=LiveCueState.PENDING).update(
                    state=LiveCueState.AIRED, fired_event=ev
                )
                if updated:
                    # 他オペレータ端末への即時通知 (#25 Phase2 D8)。best-effort (core.consumers 側で
                    # 例外を握りつぶし、ReportResult の応答には一切影響させない)。
                    broadcast_playout_update(ev.channel.slug)

        # 放確台帳 (#6 S7)。初回 DONE の CM/CM バンドルで sales の record_airing を on_commit 発火。
        # playout → sales を import しない (S6) ため Celery send_task で名前指定 (疎結合)。
        # broker 不達は reconcile_airings (日次) が回収。送出クリティカルパスに同期処理を足さない。
        if first_done and ev.action in (
            PlayoutAction.PLAY_CM,
            PlayoutAction.PLAY_CM_BUNDLE,
        ):
            ev_id = ev.id
            transaction.on_commit(lambda: _enqueue_record_airing(ev_id))
        return True


def _enqueue_record_airing(playout_event_id: int) -> None:
    from celery import current_app

    try:
        current_app.send_task("sales.tasks.record_airing", args=[playout_event_id])
    except Exception:
        logger.warning("record_airing enqueue 失敗 ev=%s (reconcile が回収)", playout_event_id)


_apply_result = sync_to_async(apply_result)


def persist_heartbeat(
    channel_slug: str,
    last_received_seq: int,
    queue_depth: int,
    caspar_health: str,
    slate_active: bool,
    feed_state: str,
    auto_return: bool,
    auto_return_suspended: bool,
    layers: list[dict] | None = None,
) -> None:
    """Heartbeat を agent_status へ upsert (docs/operations.md O7)。

    auto_return は operator トグルの意図を server が source of truth として持つ (dashboard が更新し
    SubscribeEvents が AgentControl で agent へ push する)。そのため heartbeat の auto_return は
    update では上書きしない (create_defaults = 初回 heartbeat 時のみ agent 値を採用)。
    auto_return_suspended は agent 内部のフラップ保護状態なので毎回反映する。

    **enabled の判定はここには無い。上流の _verify_token が持つ。**
    あちらは `Channel.objects.filter(slug=..., enabled=True)` で引くので、退役 channel の agent は
    そもそも認証を通れない (5 つの RPC すべてが冒頭第 1 文で _verify_token を呼ぶ)。ここで slug の
    存在しか見ていないのはそのためで、判定漏れではない。

    2026-09-06 に ch2 (enabled=False、agent_token は今も有効) の**正しい** token で Heartbeat を
    投げて実測した: ch2 は UNAUTHENTICATED "invalid channel/token"、ch1 は受理。
    退役 channel の AgentStatus 行がこの経路で復活することはない。

    ただしこの関数を gRPC 以外 (管理コマンド・新 API など) から直接呼ぶ場合は、その呼び出し側で
    enabled を見ること。ここは素通しになっている。
    """
    ch = Channel.objects.filter(slug=channel_slug).first()
    if ch is None:
        return
    common = {
        "last_heartbeat_at": timezone.now(),
        "last_received_seq": last_received_seq,
        "queue_depth": queue_depth,
        "caspar_health": caspar_health or None,
        "slate_active": slate_active,
        "feed_state": feed_state or None,
        "auto_return_suspended": auto_return_suspended,
        "layers": layers or [],  # #18 レイヤ状態スナップショット
    }
    AgentStatus.objects.update_or_create(
        channel=ch,
        defaults=common,  # update 時: operator intent (auto_return) は触らない
        create_defaults={**common, "auto_return": auto_return},  # 初回のみ agent 値を採用
    )
    # offline_notified は触らない: online↔offline の遷移検出と復帰通知は死活 beat
    # (playout.tasks.check_agent_liveness) が一元管理する (二重リセットで復帰通知が消えるのを防ぐ)。


_persist_heartbeat = sync_to_async(persist_heartbeat)


def apply_interrupt(channel_slug: str, kind: int, interrupt_key: str, at, detail: str) -> bool:
    """agent 起点の割り込み (feed 断→退避 / 復帰 / フラップ停止) を反映 (docs/operations.md O4)。

    記録は status=DONE の playout_event として残す (idempotency_key=interrupt_key で冪等)。
    O10 の配信フィルタにより DONE 行は agent へエコー配信されない (再点火を防ぐ)。
    notify() は webhook 送信を含むため、DB 更新の atomic 外で呼ぶ。
    """
    ch = Channel.objects.filter(slug=channel_slug).first()
    if ch is None:
        return False
    at = at or timezone.now()
    kinds = playout_pb2.ReportInterruptRequest.InterruptKind
    notify_args: tuple | None = None
    with transaction.atomic():
        if kind == kinds.INTERRUPT_KIND_SLATE_ON:
            PlayoutEvent.objects.get_or_create(
                idempotency_key=interrupt_key,
                defaults={
                    "channel": ch,
                    "scheduled_at": at,
                    "actual_at": at,
                    "action": PlayoutAction.PLAY_SLATE,
                    "status": PlayoutStatus.DONE,
                    "params": {"reason": detail or "feed_drop", "interrupt": True},
                },
            )
            AgentStatus.objects.filter(channel=ch).update(slate_active=True, feed_state="lost")
            msg = f"feed 断→自動スレート退避 ({detail})" if detail else "feed 断→自動スレート退避"
            notify_args = (NotificationSeverity.CRIT, "feed_lost", msg)
        elif kind == kinds.INTERRUPT_KIND_FEED_RESTORED:
            PlayoutEvent.objects.get_or_create(
                idempotency_key=interrupt_key,
                defaults={
                    "channel": ch,
                    "scheduled_at": at,
                    "actual_at": at,
                    "action": PlayoutAction.CUT_LIVE,
                    "status": PlayoutStatus.DONE,
                    "params": {"interrupt": "auto_return"},
                },
            )
            AgentStatus.objects.filter(channel=ch).update(slate_active=False, feed_state="ok")
            notify_args = (
                NotificationSeverity.INFO,
                "feed_restored",
                "feed 復旧→自動復帰で生へ復帰",
            )
        elif kind == kinds.INTERRUPT_KIND_AUTO_RETURN_SUSPENDED:
            AgentStatus.objects.filter(channel=ch).update(auto_return_suspended=True)
            notify_args = (
                NotificationSeverity.WARN,
                "auto_return_suspended",
                "フラップ上限で自動復帰をサスペンド (以後は手動復帰のみ)",
            )
        else:
            return False
    if notify_args:
        notify_mod.notify(*notify_args, channel=ch)
    return True


_apply_interrupt = sync_to_async(apply_interrupt)


def build_recording_upload(channel_slug: str, program_id: int) -> tuple[str, str] | None:
    """録画クリップの presigned PUT URL を発行 (r2_key, upload_url)。

    channel_slug/program_id の実在確認のみ行う (存在しなくても致命的ではないが、agent 側の
    誤った channel_slug 混入等を早期に検知するため軽く弾く)。r2_key は
    scheduling.live_recording.INGEST_PREFIX の規約 (ingest/live_recording/<slug>/<id>.mp4) と一致させる。
    """
    if not Channel.objects.filter(slug=channel_slug).exists():
        return None
    if not Program.objects.filter(pk=program_id).exists():
        return None
    r2_key = f"ingest/live_recording/{channel_slug}/{program_id}.mp4"
    from core import r2

    upload_url = r2.presign_put(r2_key, "video/mp4")
    return r2_key, upload_url


_build_recording_upload = sync_to_async(build_recording_upload)


class PlayoutAgentServicer(playout_pb2_grpc.PlayoutAgentServicer):
    POLL_INTERVAL_SEC = 5.0
    BATCH_LIMIT = 100

    async def SubscribeEvents(self, request, context):
        await _verify_token(context, request.channel_slug)
        cursor = request.last_known_seq
        logger.info(
            "agent subscribed channel=%s last_known_seq=%d",
            request.channel_slug,
            cursor,
        )
        last_sent_auto_return: bool | None = None
        while not context.cancelled():
            events, manifest = await _fetch_events_after(
                request.channel_slug,
                cursor,
                self.BATCH_LIMIT,
            )
            for ev in events:
                resp = playout_pb2.SubscribeEventsResponse(
                    event=_event_to_proto(ev, prefetch_manifest=manifest)
                )
                yield resp
                cursor = ev.sync_seq or cursor
            # operator トグル (agent_status.auto_return) の変化を AgentControl で push。
            # 初回 (last_sent=None) は接続時の現在値を 1 度送って agent を同期させる。
            auto_return = await _fetch_auto_return(request.channel_slug)
            if auto_return is not None and auto_return != last_sent_auto_return:
                yield playout_pb2.SubscribeEventsResponse(
                    control=playout_pb2.AgentControl(auto_return=auto_return)
                )
                last_sent_auto_return = auto_return
            await asyncio.sleep(self.POLL_INTERVAL_SEC)

    async def ReportResult(self, request, context):
        await _verify_token(context, request.channel_slug)
        status = _RESULT_PROTO_TO_DB.get(request.status)
        if status is None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unknown status")
        # proto Timestamp は UTC エポック。naive で受けると USE_TZ=True + TIME_ZONE(JST) で
        # Django が JST 解釈し 9h ずれる → aware UTC で受ける (保存時に正しい instant になる)。
        actual_at = request.actual_at.ToDatetime(tzinfo=UTC) if request.actual_at.seconds else None
        ok = await _apply_result(
            request.idempotency_key,
            status,
            actual_at,
            request.note,
        )
        if not ok:
            logger.warning(
                "ReportResult: idempotency_key=%s not found",
                request.idempotency_key,
            )
        return playout_pb2.ReportResultResponse(accepted=ok)

    async def Heartbeat(self, request, context):
        await _verify_token(context, request.channel_slug)
        logger.info(
            "agent heartbeat channel=%s seq=%d depth=%d caspar=%s feed=%s slate=%s",
            request.channel_slug,
            request.last_received_seq,
            request.queue_depth,
            request.caspar_health,
            request.feed_state,
            request.slate_active,
        )
        layers = [
            {
                "layer": ls.layer,
                "role": ls.role,
                "occupied": ls.occupied,
                "producer": ls.producer,
                "content": ls.content,
                "visible": ls.visible,
            }
            for ls in request.layers
        ]
        await _persist_heartbeat(
            request.channel_slug,
            request.last_received_seq,
            request.queue_depth,
            request.caspar_health,
            request.slate_active,
            request.feed_state,
            request.auto_return,
            request.auto_return_suspended,
            layers,
        )
        return playout_pb2.HeartbeatResponse(ok=True)

    async def ReportInterrupt(self, request, context):
        await _verify_token(context, request.channel_slug)
        at = request.at.ToDatetime(tzinfo=UTC) if request.at.seconds else None
        ok = await _apply_interrupt(
            request.channel_slug,
            request.kind,
            request.interrupt_key,
            at,
            request.detail,
        )
        if not ok:
            logger.warning(
                "ReportInterrupt: unknown channel/kind channel=%s kind=%s",
                request.channel_slug,
                request.kind,
            )
        return playout_pb2.ReportInterruptResponse(accepted=ok)

    async def RequestRecordingUpload(self, request, context):
        await _verify_token(context, request.channel_slug)
        result = await _build_recording_upload(request.channel_slug, request.program_id)
        if result is None:
            logger.warning(
                "RequestRecordingUpload: unknown channel/program channel=%s program_id=%d",
                request.channel_slug,
                request.program_id,
            )
            return playout_pb2.RequestRecordingUploadResponse()
        r2_key, upload_url = result
        return playout_pb2.RequestRecordingUploadResponse(upload_url=upload_url, r2_key=r2_key)
