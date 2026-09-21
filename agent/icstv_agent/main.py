# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""agent エントリポイント。

4 本の asyncio タスクで構成:
  1. subscribe loop: SubscribeEvents をストリーム購読 → SQLite キューに upsert/tombstone。
     AgentControl (自動復帰トグル) も同ストリームで受信し feed monitor へ反映。
  2. dispatch loop: scheduled_at が到来した event を AMCP で発火 → outbox に ReportResult。
     take 成功は feed monitor に通知 (live 区間判定用)。
  3. heartbeat loop: 30s 周期で Heartbeat (feed 状態含む) + outbox / interrupt outbox 再送。
  4. feed monitor loop: MediaMTX publisher を監視し feed 断→自動スレート / 復帰→自動戻り
     (docs/operations.md O2/O6)。

各ループは独立してリトライ。subscribe は指数バックオフで再接続 (WAN 断耐性)。
dispatch は CasparCG 未接続なら idle (起動時の caspar.connect 失敗を許容)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 生成 proto を import path に追加 (buf.gen.yaml により agent/icstv_proto/ に出力)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "icstv_proto"))

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from icstv.v1 import playout_pb2

from icstv_agent import amcp_planner, config
from icstv_agent.caspar import AmcpResult, CasparCgClient, output_is_bad
from icstv_agent.channel_media import ChannelMedia
from icstv_agent.feed_monitor import FeedMonitor
from icstv_agent.media_cache import MediaCache
from icstv_agent.mediamtx import MediaMtxClient
from icstv_agent.mirror import MODE_FILLER, MODE_ROUTE, MirrorController
from icstv_agent.overlay import OverlayManager
from icstv_agent.queue_db import QueueDb
from icstv_agent.recording import RecordingManager
from icstv_agent.server_client import ServerClient

logger = logging.getLogger("icstv_agent")

_DISPATCH_TICK_SEC = 0.1
_BACKOFF_INITIAL = 1.0
# LOADBG が失敗した event を毎 tick (100ms) 再試行すると CPU spin になる (例: filler 素材欠落で
# 404 連発)。失敗キーは次の再試行までこの秒数あけて hot loop を防ぐ。
_LOADBG_RETRY_BACKOFF = timedelta(seconds=5)
# 「これから出す clip が未到達」のときだけ slate 退避する判定窓。これより過去に予定された
# stale/孤児イベントの LOADBG/PLAY 失敗では slate を上げない (上げ続けると本編を隠すため)。
_SLATE_RECENCY = timedelta(seconds=30)
# filler/CM/CM リールの cache-miss(404) では slate 退避しない。これらは背景/挿入であり、未到達でも
# 前面 producer を維持するのが正。slate を上げると前 filler/本編を覆い PLEASE WAIT 固着→停波になる
# (2026-06-14: filler ローテーション先が未キャッシュ→404→slate 固着の根治)。本編 (PLAY_ASSET) の
# cache-miss は従来どおり slate 退避する (黒よりスレートが妥当 + Phase2 prefetch で 404 自体を防ぐ)。
# OVERLAY_OP (速報/フリーグラフィック等の手動・自動オーバーレイ操作) も別レイヤ(1-89)への操作で
# 本線(1-10)とは無関係。空レイヤへの CG STOP/CLEAR が CG エラーを返しても本線をスレートで覆っては
# ならない (2026-06-25: 速報テロップの自動 hide が手動 clear 後の空 layer40 で失敗→誤スレートの根治)。
_NO_SLATE_ON_MISS = frozenset(
    {
        playout_pb2.PLAYOUT_ACTION_PLAY_FILLER,
        playout_pb2.PLAYOUT_ACTION_PLAY_CM,
        playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
        playout_pb2.PLAYOUT_ACTION_OVERLAY_OP,
        playout_pb2.PLAYOUT_ACTION_PLAY_VT,
    }
)
_BACKOFF_MAX = 60.0

# CM バンドルの後続 reel を流す background task の参照保持 (GC されないよう退避)。
_REEL_TASKS: set[asyncio.Task] = set()

# take 成功後に reel (後続 AUTO 連結) を background spawn する対象 action。
# PLAY_CM_BUNDLE (CM リール) と PLAY_VT (生番組内 VT ロール、末尾に生復帰ステップを持ち得る) の
# 双方が plan.reel を持つ (#25 §6.5)。
_REEL_ACTIONS: frozenset[int] = frozenset(
    {
        playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
        playout_pb2.PLAYOUT_ACTION_PLAY_VT,
    }
)

# exposure_policy (#27、docs/site-only-broadcast.md §4.4)。YTミラー制御は plan() を経由せず
# _do_loadbg/_do_take の冒頭で action 判定して MirrorController(seg 別)へ委譲する
# (slate/yt_transition と同じ側道流儀)。対象ミラーは params["mirror_seg"] (yt_public/yt_members)
# で識別する (proto に seg フィールドは無い。resolver 側で params に積んでいる)。
_MIRROR_ACTIONS: frozenset[int] = frozenset(
    {
        playout_pb2.PLAYOUT_ACTION_YT_MIRROR_FILLER,
        playout_pb2.PLAYOUT_ACTION_YT_MIRROR_ROUTE,
    }
)
_MIRROR_ACTION_NAMES = {
    playout_pb2.PLAYOUT_ACTION_YT_MIRROR_FILLER: "yt_mirror_filler",
    playout_pb2.PLAYOUT_ACTION_YT_MIRROR_ROUTE: "yt_mirror_route",
}


# ---- subscribe ----


def _apply_event(queue: QueueDb, event: playout_pb2.PlayoutEvent) -> None:
    if event.tombstone:
        queue.remove_event(event.idempotency_key)
        logger.info("tombstone key=%s seq=%d", event.idempotency_key, event.sync_seq)
        return
    sched_at = event.scheduled_at.ToDatetime(tzinfo=UTC)
    queue.upsert_event(
        idempotency_key=event.idempotency_key,
        sync_seq=event.sync_seq,
        scheduled_at=sched_at,
        action=event.action,
        payload=event.SerializeToString(),
    )
    logger.info(
        "upsert key=%s seq=%d action=%d sched=%s",
        event.idempotency_key,
        event.sync_seq,
        event.action,
        sched_at.isoformat(),
    )


def _apply_control(queue: QueueDb, monitor: FeedMonitor, control) -> None:
    """server → agent の AgentControl を反映 (#7 O4)。auto_return はローカル永続化。"""
    if control.HasField("auto_return"):
        monitor.on_agent_control(control.auto_return)
        queue.set_state("auto_return", "true" if control.auto_return else "false")
        logger.info("agent control: auto_return=%s", control.auto_return)


async def _subscribe_loop(
    client: ServerClient, queue: QueueDb, channel_slug: str, monitor: FeedMonitor
) -> None:
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            last_seq = queue.last_known_seq()
            logger.info(
                "subscribe stream connect channel=%s last_seq=%d",
                channel_slug,
                last_seq,
            )
            async for resp in client.subscribe_events(channel_slug, last_seq):
                if resp.HasField("event"):
                    _apply_event(queue, resp.event)
                elif resp.HasField("control"):
                    _apply_control(queue, monitor, resp.control)
            backoff = _BACKOFF_INITIAL  # 正常クローズ → リセット
        except grpc.aio.AioRpcError as e:
            logger.warning("subscribe RPC error %s — retry in %.1fs", e.code(), backoff)
        except Exception:
            logger.exception("subscribe loop unexpected error")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)


# ---- dispatch ----


def _enqueue_result(
    queue: QueueDb,
    channel_slug: str,
    idempotency_key: str,
    status: int,
    when: datetime,
    note: str = "",
) -> None:
    ts = Timestamp()
    ts.FromDatetime(when)
    req = playout_pb2.ReportResultRequest(
        channel_slug=channel_slug,
        idempotency_key=idempotency_key,
        status=status,
        actual_at=ts,
        note=note,
    )
    queue.enqueue_outbox(idempotency_key, req.SerializeToString())


def _parse_event(entry: dict) -> playout_pb2.PlayoutEvent:
    ev = playout_pb2.PlayoutEvent()
    ev.ParseFromString(entry["payload"])
    return ev


def _off_air_slate_expired(ev: playout_pb2.PlayoutEvent, now: datetime) -> bool:
    """resolver 管轄の休止スレート (params off_air=True) が有効期限を過ぎているか。

    休止中は resolver が周期ごとにスレートを再発行するため、休止明けの直前に発行された
    PLAY_SLATE が gRPC 到達 + ディスパッチ遅延 (実測 1〜6 秒) の間に休止明けの CLEAR_SLATE
    に追い越されることがある。そのまま撃つと復帰済みの本線 (layer 10) の上へスレート
    (layer 90) を再点灯させ、次の CLEAR まで固着する (2026-07-21 06:00: 05:59:59 発行の
    スレートが 06:00:01 に実行され、運用者が手動解除する 08:21 まで 2h21m スレート固着)。
    params["until"] (= 休止終了時刻) を過ぎていたら AMCP を撃たない。

    手動/緊急スレート (off_air param 無し) は運用者の意図なので期限を持たず、対象外。
    """
    if ev.action != playout_pb2.PLAYOUT_ACTION_PLAY_SLATE:
        return False
    params = ev.params
    if params.get("off_air", "").lower() not in ("true", "1"):
        return False
    until = params.get("until", "")
    if not until:
        return False
    try:
        deadline = datetime.fromisoformat(until)
    except ValueError:
        logger.warning("休止スレートの until を解釈できない: %r", until)
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return now >= deadline


async def _try_reconnect(caspar: CasparCgClient) -> None:
    if not caspar.is_connected:
        await caspar.connect()


# casparcg 再接続時に本線へ貼り直す対象 action (持続する本線状態のみ)。
# CM/bundle/slate/yt は本線の持続状態ではないので除外 (再 take でループさせない)。
_RETAKE_ACTIONS: tuple[int, ...] = (
    playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
    playout_pb2.PLAYOUT_ACTION_CUT_LIVE,
    playout_pb2.PLAYOUT_ACTION_PLAY_FILLER,
)


async def _retake_current(
    queue: QueueDb,
    caspar: CasparCgClient,
    cfg: config.Config,
    *,
    reason: str = "caspar 再接続",
    overlay: OverlayManager | None = None,
) -> bool:
    """現行イベントを本線 layer へ貼り直す (#7 再起動黒落ち対策 / 出力 watchdog 共用)。

    casparcg を再起動すると LOADBG 済み背面も前面 PLAY も消えるが、現行イベントは既に
    executed 済みで due_for_take に再び乗らないため、次の予定 TAKE まで黒落ちする。再接続や
    出力異常 (黒/フリーズ) を検知したら直近 executed の本線イベントを clip 付き PLAY で即時に
    貼り直す。対象が無ければ何もしない。本線送出を止めないため best-effort (例外は握り潰す)。
    戻り値=再 take を発行したか。
    """
    entry = queue.current_main_event(_RETAKE_ACTIONS)
    if entry is None:
        logger.info("%s: 現行本線イベント無し — 再 take せず次の予定 TAKE を待つ", reason)
        return False
    ev = _parse_event(entry)
    try:
        cmds = amcp_planner.retake_plan(ev, channel=cfg.caspar_channel)
    except ValueError:
        logger.exception("retake planner error key=%s", entry["idempotency_key"])
        return False
    if not cmds:
        return False
    logger.info(
        "%s: 現行イベント key=%s action=%d を本線へ再 take",
        reason,
        entry["idempotency_key"],
        entry["action"],
    )
    for cmd in cmds:
        try:
            await caspar.amcp(cmd)
        except Exception:
            logger.exception("再 take コマンド失敗 key=%s cmd=%s", entry["idempotency_key"], cmd)
            return False
    # casparcg 再起動で CG レイヤーも消えているので、Lバー/予告を再 ADD して復帰する。
    if overlay is not None:
        with contextlib.suppress(Exception):
            await overlay.restore(ev.action, dict(ev.params))
    return True


async def _retake_slate(
    caspar: CasparCgClient,
    cfg: config.Config,
    channel_media: ChannelMedia,
    monitor: FeedMonitor,
    *,
    reason: str = "caspar 再接続",
) -> bool:
    """スレートが出ているべき状態なら slate layer へ貼り直す (#7 再起動黒落ち対策)。

    casparcg を再起動すると本線と同様に slate layer (N-90) も消える。本線だけを貼り直すと、
    スレートで隠されていたはずの本線が露出してしまう。編成の休止帯スレートは 5 分周期で
    再発行されるため最大 5 分で自然復帰するが、その間は「休止中」ではなくフィラー本編が
    映る。手動/緊急スレートと feed 断退避には再発行が無く、消えたまま戻らない。

    monitor.slate_active は PLAY_SLATE/CLEAR_SLATE イベントと feed 断退避の双方で更新される
    ので、「いまスレートが出ているべきか」の判定はこれで足りる。clip は goto_slate と同じ
    channel_media.slate_clip を使う (manifest 由来、未着ならノードローカルの please_wait)。

    本線送出を止めないため best-effort (例外は握り潰す)。戻り値=貼り直したか。
    """
    if not monitor.slate_active:
        return False
    logger.info("%s: スレート表示中のため slate layer を貼り直す", reason)
    try:
        await caspar.amcp(amcp_planner.slate_command(cfg.caspar_channel, channel_media.slate_clip))
    except Exception:
        logger.exception("スレート再 take 失敗")
        return False
    return True


# 黒フェード (dip to black) 対象になり得る incoming action。CUT_LIVE は自前の MIX を持つので除外、
# CM_BUNDLE は先頭が MIX で残りハードカットの独自挙動なので除外。
_DIP_INCOMING: frozenset[int] = frozenset(
    {
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        playout_pb2.PLAYOUT_ACTION_PLAY_CM,
        playout_pb2.PLAYOUT_ACTION_PLAY_FILLER,
    }
)


def _should_dip(queue: QueueDb, ev: playout_pb2.PlayoutEvent, cfg: config.Config) -> bool:
    """この take をフィラー絡みの黒フェードで繋ぐべきか (適用範囲=フィラー境界のみ)。

    transition_ms>0 かつ「フィラー↔非フィラー」を跨ぐ切替のときだけ True。番組同士/CM の
    ハードカット既定 (docs/casparcg.md §2.4) は維持する。outgoing は直近 executed の本線
    持続イベント (current_main_event) で判定する (incoming はまだ mark_executed 前)。
    """
    if getattr(cfg, "transition_ms", 0) <= 0:
        return False
    incoming = ev.action
    if incoming not in _DIP_INCOMING:
        return False
    cur = queue.current_main_event(_RETAKE_ACTIONS)
    outgoing = cur["action"] if cur else None
    filler = playout_pb2.PLAYOUT_ACTION_PLAY_FILLER
    return (incoming == filler) != (outgoing == filler)  # XOR: フィラー境界のみ


async def _restore_main_levels(caspar: CasparCgClient, cfg: config.Config) -> None:
    """黒フェード途中でテイク失敗した際、本線を黒+無音のまま残さないよう即時復帰 (best-effort)。"""
    for cmd in amcp_planner.main_reset_levels_cmds(cfg.caspar_channel):
        with contextlib.suppress(Exception):
            await caspar.amcp(cmd)


async def _do_loadbg(
    queue: QueueDb,
    caspar: CasparCgClient,
    entry: dict,
    cfg: config.Config,
    now: datetime,
    channel_media: ChannelMedia,
    mirrors: dict[str, MirrorController] | None = None,
) -> bool:
    """LOADBG 段: 背面ロード。戻り値=処理完了(True)/要再試行(False)。

    失敗 (False) のときは呼び出し側が backoff を入れて再試行する (毎 tick の hot loop を防ぐ)。
    """
    ev = _parse_event(entry)
    if ev.action in _MIRROR_ACTIONS:
        # YTミラーは LOADBG 不要 (plan.loadbg is None の既存経路と同型)、take 段で処理する。
        queue.mark_loaded(entry["idempotency_key"])
        return True
    try:
        plan = amcp_planner.plan(
            ev,
            channel=cfg.caspar_channel,
            fps=cfg.fps,
            slate_clip=channel_media.slate_clip,
        )
    except ValueError:
        logger.exception("planner error key=%s", entry["idempotency_key"])
        return False

    if plan.loadbg is None:
        # play_slate / yt_transition は LOADBG 不要、即時段で扱う
        queue.mark_loaded(entry["idempotency_key"])
        return True

    try:
        result: AmcpResult = await caspar.amcp(plan.loadbg)
    except Exception:
        logger.exception("LOADBG send error key=%s", entry["idempotency_key"])
        return False

    if result.is_success:
        queue.mark_loaded(entry["idempotency_key"])
        return True

    # 404/502 は素材未到達 → スレート退避を試みる (次 tick の take 段で再評価)
    logger.warning(
        "LOADBG failed key=%s code=%d header=%s",
        entry["idempotency_key"],
        result.code,
        result.header,
    )
    # 直近に予定された (=これから出す) clip の未到達のときだけ slate 退避。過去に予定された
    # stale/孤児イベントや filler/CM (背景/挿入) では slate を上げない (前面を覆い隠さない)。
    if (
        result.code in (404, 502)
        and (now - entry["scheduled_at"]) <= _SLATE_RECENCY
        and ev.action not in _NO_SLATE_ON_MISS
    ):
        with contextlib.suppress(Exception):
            await caspar.amcp(
                amcp_planner.slate_command(cfg.caspar_channel, channel_media.slate_clip)
            )
    return False


async def _run_reel(caspar: CasparCgClient, reel: tuple[tuple[str, int], ...]) -> None:
    """CM バンドルの 2 本目以降を AUTO で逐次連結する (先頭は _do_take が PLAY 済み)。

    各ステップ: 後続 clip を LOADBG ... AUTO で背面に積み、いま前面の clip の尺だけ
    待つ。前面終了で CasparCG が背面へ自動切替 → 次ステップでさらに後続を積む。
    AMCP の AUTO は 1 段ずつしか積めないため「前面再生中に 1 本だけ先読み」を繰り返す。
    feed 断/slate 割り込みは layer 別なので reel とは独立 (best-effort、例外は握り潰す)。
    """
    for cmd, hold_ms in reel:
        with contextlib.suppress(Exception):
            await caspar.amcp(cmd)
        await asyncio.sleep(max(0.0, hold_ms / 1000))


async def _run_cg(caspar: CasparCgClient, cg: tuple[str, ...]) -> None:
    """CG オーバーレイ (提供クレジット等) を発射。本線とは別 layer の best-effort。"""
    for cmd in cg:
        try:
            await caspar.amcp(cmd)
        except Exception:
            logger.warning("CG コマンド失敗 (best-effort): %s", cmd)


def _spawn_cg(caspar: CasparCgClient, cg: tuple[str, ...]) -> None:
    task = asyncio.create_task(_run_cg(caspar, cg))
    _REEL_TASKS.add(task)
    task.add_done_callback(_REEL_TASKS.discard)


async def _http_download(url: str, dest: Path) -> None:
    """presigned URL を媒体ファイルへストリームダウンロード (prefetch)。"""
    import httpx

    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as http,
        http.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=1 << 20):
                f.write(chunk)


async def _prefetch_loop(
    queue: QueueDb, cache: MediaCache, cfg: config.Config, channel_media: ChannelMedia
) -> None:
    """編成の N 秒先までの event の媒体を R2 からローカルへ先読みする (overview 3.2)。

    LOADBG が参照する clip 実体を事前に媒体フォルダへ置く。media_url の無い event はスキップ。
    失敗は best-effort (本線送出に影響させず次周で再試行)。

    候補は「未来の未実行 event」に加え「直近受信した event (実行済み含む)」も対象にする。
    filler は just-in-time 発行で受信即 executed され未来候補から漏れるが、同じ clip が反復する
    ため、遅れてでも prefetch すれば次回の LOADBG に間に合う。再取得は ensure が冪等 (既存なら
    mtime 更新のみ) なので反復 clip は LRU 退避されず実質ピン留めになる (自己修復)。"""
    while True:
        try:
            until = datetime.now(UTC) + timedelta(seconds=cfg.prefetch_ahead_sec)
            entries = queue.prefetch_candidates(until, limit=cfg.prefetch_limit)
            entries += queue.recently_received(cfg.prefetch_recent_sec, limit=cfg.prefetch_limit)
            seen: set[str] = set()
            manifest_json: str | None = None
            for entry in entries:
                key = entry["idempotency_key"]
                if key in seen:
                    continue
                seen.add(key)
                ev = _parse_event(entry)
                params = dict(ev.params)
                # clip は typed payload を優先 (params はフォールバック)、media_url は補助 params
                if manifest_json is None:
                    manifest_json = params.get("prefetch_manifest")
                clip = amcp_planner.clip_of(ev) or params.get("clip")
                url = params.get("media_url")
                if url and clip:
                    await cache.ensure(clip, url)
            # standing メディア manifest (channel の filler 全件 + slate) を pre-cache+pin する。
            # 巡回先が常に手元に在る状態を作り、未キャッシュ→LOADBG 404→slate 固着 を根絶 (Phase2)。
            if manifest_json:
                await _prefetch_manifest(cache, manifest_json, channel_media)
        except Exception:
            logger.exception("prefetch loop error")
        await asyncio.sleep(cfg.prefetch_poll_sec)


async def _prefetch_manifest(
    cache: MediaCache, manifest_json: str, channel_media: ChannelMedia
) -> None:
    """server の prefetch manifest (JSON: [{clip, url, slate?}]) を全件 pre-cache+pin する。

    pin により standing メディアは LRU 退避されず常駐する。slate 項目があれば channel_media に
    per-channel slate clip を反映し、退避時に使う (無ければ既定へフォールバック)。
    失敗は best-effort (次周で再試行)。
    """
    try:
        items = json.loads(manifest_json)
    except (ValueError, TypeError):
        logger.warning("prefetch manifest の JSON 解析失敗")
        return
    slate_clip: str | None = None
    site_only_filler_clip: str | None = None
    members_filler_clip: str | None = None
    for item in items:
        clip = item.get("clip")
        url = item.get("url")
        if clip and url:
            await cache.ensure_pinned(clip, url)
        if item.get("slate"):
            slate_clip = clip
        if item.get("site_only_filler"):
            site_only_filler_clip = clip
        if item.get("members_filler"):
            members_filler_clip = clip
    channel_media.set_slate_clip(slate_clip)
    channel_media.set_site_only_filler_clip(site_only_filler_clip)
    channel_media.set_members_filler_clip(members_filler_clip)


async def _output_watchdog(
    queue: QueueDb,
    caspar: CasparCgClient,
    monitor: FeedMonitor,
    cfg: config.Config,
) -> None:
    """本線 foreground の出力健全を監視し、黒(empty)/フリーズを検知したら再 take する (#7)。

    「AMCP は健全と言うが出力が黒/静止」という盲点を CasparCG 側で自動復旧する。連続
    watchdog_bad_ticks 回 bad で 1 度だけ再 take してカウンタを戻す (連打防止)。スレート中
    (feed断退避/手動緊急) は出力がスレートで意図的なので skip。INFO 取得失敗は判定不能で skip。
    """
    bad = 0
    prev_name: str | None = None
    prev_time: float | None = None
    while True:
        await asyncio.sleep(cfg.watchdog_poll_sec)
        try:
            if not caspar.is_connected or monitor.slate_active:
                bad = 0
                continue
            fg = await caspar.layer_foreground(cfg.caspar_channel, amcp_planner.LAYER_MAIN)
            if output_is_bad(fg, prev_name, prev_time):
                bad += 1
                if bad >= cfg.watchdog_bad_ticks:
                    state = "empty" if (fg and fg["producer"] == "empty") else "frozen"
                    logger.warning("出力 watchdog: 本線が %s (%d tick 連続) — 再 take", state, bad)
                    await _retake_current(queue, caspar, cfg, reason=f"出力 watchdog ({state})")
                    bad = 0
            else:
                bad = 0
            if fg is not None:
                prev_name, prev_time = fg["name"], fg["time"]
        except Exception:
            logger.exception("output watchdog loop error")


async def _recording_upload_retry_loop(recording: RecordingManager, cfg: config.Config) -> None:
    """録画クリップの store-and-forward 再送 (WAN 断/アップロード失敗時に outbox へ退避した分)。

    本線送出とは独立したループなので、失敗しても他ループには影響しない (best-effort)。
    """
    while True:
        try:
            await recording.retry_pending_uploads()
        except Exception:
            logger.exception("recording upload retry loop error")
        await asyncio.sleep(cfg.recording_upload_poll_sec)


def _spawn_reel(caspar: CasparCgClient, reel: tuple[tuple[str, int], ...]) -> None:
    """reel を dispatch loop をブロックしない background task で流す。"""
    task = asyncio.create_task(_run_reel(caspar, reel))
    _REEL_TASKS.add(task)
    task.add_done_callback(_REEL_TASKS.discard)


async def _do_take(
    queue: QueueDb,
    caspar: CasparCgClient,
    entry: dict,
    cfg: config.Config,
    channel_slug: str,
    monitor: FeedMonitor,
    channel_media: ChannelMedia,
    overlay: OverlayManager | None = None,
    mirrors: dict[str, MirrorController] | None = None,
) -> None:
    """TAKE 段: 背面 → 前面。成功で as-run を outbox へ。失敗は goto_slate。"""
    ev = _parse_event(entry)
    # 期限切れの休止スレートは AMCP を撃たずに畳む (復帰済み本線への再点灯=固着の防止)。
    now = datetime.now(UTC)
    if _off_air_slate_expired(ev, now):
        logger.info(
            "休止明けを過ぎた off_air スレートを SKIP key=%s until=%s",
            entry["idempotency_key"],
            ev.params.get("until", ""),
        )
        queue.mark_executed(entry["idempotency_key"], now)
        _enqueue_result(
            queue,
            channel_slug,
            entry["idempotency_key"],
            playout_pb2.RESULT_STATUS_SKIPPED,
            now,
            note="off_air slate expired (休止明け後の到着)",
        )
        return
    if ev.action in _MIRROR_ACTIONS:
        # YTミラーは plan() を経由せず MirrorController へ直接委譲する (side-channel)。
        controller = (mirrors or {}).get(ev.params.get("mirror_seg", ""))
        actual_at = datetime.now(UTC)
        if controller is not None:
            await controller.handle_event(_MIRROR_ACTION_NAMES[ev.action])
        queue.mark_executed(entry["idempotency_key"], actual_at)
        _enqueue_result(
            queue,
            channel_slug,
            entry["idempotency_key"],
            playout_pb2.RESULT_STATUS_DONE,
            actual_at,
            note="yt mirror",
        )
        return
    try:
        plan = amcp_planner.plan(
            ev,
            channel=cfg.caspar_channel,
            fps=cfg.fps,
            slate_clip=channel_media.slate_clip,
        )
    except ValueError:
        return

    actual_at = datetime.now(UTC)
    if not plan.take:
        # yt_transition は agent では no-op で完了扱い
        queue.mark_executed(entry["idempotency_key"], actual_at)
        _enqueue_result(
            queue,
            channel_slug,
            entry["idempotency_key"],
            playout_pb2.RESULT_STATUS_SKIPPED,
            actual_at,
            note="agent no-op",
        )
        return

    # フィラー絡みの切替は黒フェード: 本線(N-10)を黒+無音へ落としきってからテイクし、
    # テイク後に level=1.0 へ戻して番組をフェードインする (docs/casparcg.md §1.3, §2.4)。
    dip = _should_dip(queue, ev, cfg)
    dip_frames = amcp_planner.ms_to_frames(cfg.transition_ms, cfg.fps) if dip else 0
    if dip:
        for cmd in amcp_planner.main_dip_cmds(cfg.caspar_channel, level=0.0, frames=dip_frames):
            with contextlib.suppress(Exception):
                await caspar.amcp(cmd)
        await asyncio.sleep(cfg.transition_ms / 1000)  # 黒に落ちきってからテイク
        actual_at = datetime.now(UTC)  # フェードアウト分の遅延を反映したテイク時刻

    try:
        result = await caspar.amcp(plan.take)
    except Exception as e:
        logger.exception("PLAY send error key=%s", entry["idempotency_key"])
        if dip:
            await _restore_main_levels(caspar, cfg)  # 黒+無音のまま残さない
        _enqueue_result(
            queue,
            channel_slug,
            entry["idempotency_key"],
            playout_pb2.RESULT_STATUS_FAILED,
            actual_at,
            note=str(e),
        )
        return

    if result.is_success:
        # 黒に落とした本線を通常へ戻して番組/フィラーをフェードイン (テイク済み producer に作用)。
        if dip:
            for cmd in amcp_planner.main_dip_cmds(cfg.caspar_channel, level=1.0, frames=dip_frames):
                with contextlib.suppress(Exception):
                    await caspar.amcp(cmd)
        # feed monitor に take を通知 (cut_live→live 区間 ON / play_asset・filler→OFF /
        # play_slate・clear_slate→スレート調停。docs/operations.md O6)。
        monitor.on_event_taken(ev.action, dict(ev.params))
        # CM バンドル/VT ロールは先頭 take 成功後、後続 reel を background で逐次連結する。
        if ev.action in _REEL_ACTIONS and plan.reel:
            _spawn_reel(caspar, plan.reel)
        # 提供クレジット等の CG オーバーレイを別 layer へ best-effort 発射 (本線に影響させない)。
        if plan.cg:
            _spawn_cg(caspar, plan.cg)
        # Lバー(1-30)/次番組予告(1-40) の常駐オーバーレイを調停 (別 layer, best-effort)。
        if overlay is not None:
            with contextlib.suppress(Exception):
                await overlay.on_event(ev.action, dict(ev.params))
        queue.mark_executed(entry["idempotency_key"], actual_at)
        _enqueue_result(
            queue,
            channel_slug,
            entry["idempotency_key"],
            playout_pb2.RESULT_STATUS_DONE,
            actual_at,
        )
        return

    # PLAY 失敗 → FAILED 報告。slate 退避は直近予定 (これから出す) の本編のみ
    # (stale/孤児や filler/CM の PLAY 失敗で前面を覆い隠さない)。
    logger.warning(
        "PLAY failed key=%s code=%d header=%s",
        entry["idempotency_key"],
        result.code,
        result.header,
    )
    if dip:
        await _restore_main_levels(caspar, cfg)  # 黒+無音のまま残さない (slate 解除後に備える)
    if (actual_at - entry["scheduled_at"]) <= _SLATE_RECENCY and ev.action not in _NO_SLATE_ON_MISS:
        with contextlib.suppress(Exception):
            await caspar.amcp(
                amcp_planner.slate_command(cfg.caspar_channel, channel_media.slate_clip)
            )
    queue.mark_executed(entry["idempotency_key"], actual_at)
    _enqueue_result(
        queue,
        channel_slug,
        entry["idempotency_key"],
        playout_pb2.RESULT_STATUS_FAILED,
        actual_at,
        note=result.header,
    )


async def _dispatch_loop(
    queue: QueueDb,
    caspar: CasparCgClient,
    channel_slug: str,
    cfg: config.Config,
    monitor: FeedMonitor,
    channel_media: ChannelMedia,
    overlay: OverlayManager | None = None,
    mirrors: dict[str, MirrorController] | None = None,
) -> None:
    """100ms tick で 2 段ディスパッチ (LOADBG → PLAY)。

    LOADBG: scheduled_at - PREROLL <= now かつ loaded_at IS NULL の event
    PLAY:   scheduled_at <= now かつ executed_at IS NULL の event
    失敗時は goto_slate に退避し as-run を FAILED で報告。
    """
    loadbg_backoff: dict[str, datetime] = {}  # key -> 次に再試行してよい時刻 (失敗時のみ)
    # 起動時に caspar.connect 済みなら再 take 不要 (現行が貼られている)。未接続起動なら
    # 後の初回接続成功時に「切断→接続」遷移として現行を貼り直す。
    while True:
        await asyncio.sleep(_DISPATCH_TICK_SEC)
        if not caspar.is_connected:
            # 1 tick おきに再接続を試みる
            await _try_reconnect(caspar)
            if caspar.is_connected:
                # 切断→再接続: casparcg 再起動で本線が消えている可能性 → 現行を貼り直す
                # (再起動時の黒落ち解消。次の予定 TAKE を待たない)。CG も再 ADD で復帰させる。
                #
                # 発火条件は「切断を観測した」ことだけに依存させる。直前の接続状態を
                # 持ち回して「再接続を一度は失敗した」ことまで求めると、casparcg が
                # 即座に AMCP ポートを再バインドして初回の再接続が成功したケースを
                # 取りこぼす (2026-08-21 のリサイクルで 35 分の黒落ち)。
                await _retake_current(queue, caspar, cfg, overlay=overlay)
                # 本線だけ戻してスレートを戻さないと、休止帯や緊急退避中に隠されていた
                # 本線が露出する。slate layer も同じ再接続で復帰させる。
                await _retake_slate(caspar, cfg, channel_media, monitor)
                for controller in (mirrors or {}).values():
                    await controller.reapply()
            continue
        now = datetime.now(UTC)

        for entry in queue.due_for_loadbg(now, preroll_sec=cfg.preroll_sec, limit=4):
            key = entry["idempotency_key"]
            retry_at = loadbg_backoff.get(key)
            if retry_at is not None and now < retry_at:
                continue  # 直近の失敗から backoff 中
            if await _do_loadbg(queue, caspar, entry, cfg, now, channel_media, mirrors):
                loadbg_backoff.pop(key, None)
            else:
                loadbg_backoff[key] = now + _LOADBG_RETRY_BACKOFF

        for entry in queue.due_for_take(now, limit=4):
            await _do_take(
                queue,
                caspar,
                entry,
                cfg,
                channel_slug,
                monitor,
                channel_media,
                overlay,
                mirrors,
            )


# ---- heartbeat + outbox ----


async def _flush_outbox(client: ServerClient, queue: QueueDb) -> None:
    for entry in queue.outbox_iter(limit=10):
        req = playout_pb2.ReportResultRequest()
        req.ParseFromString(entry["payload"])
        actual_at = (
            req.actual_at.ToDatetime(tzinfo=UTC) if req.actual_at.seconds else datetime.now(UTC)
        )
        try:
            ok = await client.report_result(
                channel_slug=req.channel_slug,
                idempotency_key=req.idempotency_key,
                status=req.status,
                actual_at=actual_at,
                note=req.note,
            )
            if ok:
                queue.outbox_delete(req.idempotency_key)
            else:
                queue.outbox_bump(req.idempotency_key)
        except grpc.aio.AioRpcError as e:
            queue.outbox_bump(req.idempotency_key)
            logger.warning("outbox flush RPC error %s — leave in outbox", e.code())
            return  # 接続不可なら今回は打ち切り (次 heartbeat で再試行)


async def _flush_interrupt_outbox(client: ServerClient, queue: QueueDb) -> None:
    """ReportInterrupt の store-and-forward 再送 (WAN 断時にバッファした割り込み)。"""
    for entry in queue.interrupt_outbox_iter(limit=10):
        req = playout_pb2.ReportInterruptRequest()
        req.ParseFromString(entry["payload"])
        at = req.at.ToDatetime(tzinfo=UTC) if req.at.seconds else datetime.now(UTC)
        try:
            ok = await client.report_interrupt(
                channel_slug=req.channel_slug,
                interrupt_key=req.interrupt_key,
                kind=req.kind,
                at=at,
                detail=req.detail,
            )
            if ok:
                queue.interrupt_outbox_delete(req.interrupt_key)
            else:
                queue.interrupt_outbox_bump(req.interrupt_key)
        except grpc.aio.AioRpcError as e:
            queue.interrupt_outbox_bump(req.interrupt_key)
            logger.warning("interrupt outbox flush RPC error %s", e.code())
            return


async def _collect_layer_states(
    caspar: CasparCgClient, channel: int, overlay: OverlayManager | None
) -> list[dict]:
    """各レイヤの状態を INFO + overlay 内部状態から構築 (#18 §A レイヤ状態パネル用)。"""
    snap = overlay.snapshot() if overlay is not None else {}
    layers = list(amcp_planner.LAYER_ROLES)
    info = await caspar.channel_layers(channel, layers)  # INFO {ch} 1 回で層別に切る
    states: list[dict] = []
    for layer, role in amcp_planner.LAYER_ROLES.items():
        fg = info.get(layer, {})
        producer = fg.get("producer") or "empty"
        name = fg.get("name") or ""
        occupied = producer not in ("empty", "")
        ov = snap.get(layer)  # 管理レイヤ(Lバー/予告)は内部状態を優先
        states.append(
            {
                "layer": layer,
                "role": role,
                "occupied": occupied or ov is not None,
                "producer": producer,
                "content": ov["content"] if ov else name,
                "visible": ov["visible"] if ov else occupied,
            }
        )
    return states


async def _heartbeat_loop(
    client: ServerClient,
    queue: QueueDb,
    caspar: CasparCgClient,
    channel_slug: str,
    interval: int,
    monitor: FeedMonitor,
    caspar_channel: int = 1,
    overlay: OverlayManager | None = None,
) -> None:
    while True:
        try:
            last_seq = queue.last_known_seq()
            depth = queue.pending_count()
            health = await caspar.health()
            layers = await _collect_layer_states(caspar, caspar_channel, overlay)
            await client.heartbeat(
                channel_slug=channel_slug,
                last_received_seq=last_seq,
                queue_depth=depth,
                caspar_health=health,
                slate_active=monitor.slate_active,
                feed_state=monitor.feed_state,
                auto_return=monitor.auto_return,
                auto_return_suspended=monitor.auto_return_suspended,
                layers=layers,
            )
            await _flush_outbox(client, queue)
            await _flush_interrupt_outbox(client, queue)
        except grpc.aio.AioRpcError as e:
            logger.warning("heartbeat RPC error %s", e.code())
        except Exception:
            logger.exception("heartbeat loop unexpected error")
        await asyncio.sleep(interval)


# ---- entry ----


async def run() -> None:
    cfg = config.load()
    queue = QueueDb(cfg.queue_db_path)
    client = ServerClient(cfg.server_target, cfg.auth_token)
    caspar = CasparCgClient(cfg.caspar_host, cfg.caspar_port)
    mtx = MediaMtxClient(cfg.mediamtx_api_url)
    cache = MediaCache(cfg.media_dir, cfg.cache_max_bytes, _http_download)
    # per-channel slate の共有ホルダ。prefetch loop が manifest の slate で更新し、slate 退避
    # コード (feed_monitor / dispatch) が参照する。未設定/未着は cfg.slate_clip (please_wait)。
    channel_media = ChannelMedia(cfg.slate_clip, cfg.site_only_filler_clip, cfg.members_filler_clip)
    overlay = OverlayManager(caspar, channel=cfg.caspar_channel)  # Lバー/次番組予告の常駐管理
    # exposure_policy (#27)。mirror_channel が未設定 (None) の seg は全メソッド no-op になり
    # 既存 1ch/2ch ノードを壊さない (このノードにミラー channel を切っていなければ何もしない)。
    mirrors: dict[str, MirrorController] = {
        "yt_public": MirrorController(
            seg="yt_public",
            mirror_channel=cfg.yt_mirror_caspar_channel,
            main_channel=cfg.caspar_channel,
            default_mode=MODE_ROUTE,
            filler_clip_getter=lambda: channel_media.site_only_filler_clip,
            queue=queue,
            caspar=caspar,
        ),
        "yt_members": MirrorController(
            seg="yt_members",
            mirror_channel=cfg.yt_members_caspar_channel,
            main_channel=cfg.caspar_channel,
            default_mode=MODE_FILLER,
            filler_clip_getter=lambda: channel_media.members_filler_clip,
            queue=queue,
            caspar=caspar,
        ),
    }
    # 生放送録画 (record_live)。I/O (MediaMTX API / gRPC / PUT) は全て feed_monitor から
    # background task として呼ばれ、本線送出 (AMCP) を絶対にブロック/失敗させない (best-effort)。
    recording = RecordingManager(
        mediamtx=mtx,
        server_client=client,
        queue=queue,
        recording_dir=cfg.recording_dir,
        min_free_bytes=cfg.recording_min_free_bytes,
    )

    # auto_return は再起動でも維持 (ローカル永続)。未設定なら既定 ON。
    auto_return = queue.get_state("auto_return", "true") != "false"

    async def send_interrupt(kind: int, detail: str) -> None:
        """feed monitor 起点の割り込み報告。interrupt outbox に積んで即時送信を試みる。"""
        key = str(uuid.uuid4())
        at = datetime.now(UTC)
        ts = Timestamp()
        ts.FromDatetime(at)
        req = playout_pb2.ReportInterruptRequest(
            channel_slug=cfg.channel_slug,
            interrupt_key=key,
            kind=kind,
            at=ts,
            detail=detail,
        )
        queue.enqueue_interrupt(key, req.SerializeToString())  # store-and-forward
        try:
            ok = await client.report_interrupt(
                channel_slug=cfg.channel_slug,
                interrupt_key=key,
                kind=kind,
                at=at,
                detail=detail,
            )
            if ok:
                queue.interrupt_outbox_delete(key)
        except grpc.aio.AioRpcError as e:
            logger.warning("report_interrupt 即時送信失敗 %s — interrupt outbox に残す", e.code())

    monitor = FeedMonitor(
        caspar=caspar,
        mediamtx=mtx,
        send_interrupt=send_interrupt,
        channel=cfg.caspar_channel,
        channel_media=channel_media,
        channel_slug=cfg.channel_slug,
        poll_sec=cfg.feed_poll_sec,
        lost_ticks=cfg.feed_lost_ticks,
        hysteresis_ticks=cfg.feed_hysteresis_ticks,
        flap_window_sec=cfg.feed_flap_window_sec,
        flap_max=cfg.feed_flap_max,
        auto_return=auto_return,
        recording=recording,
    )

    await client.connect()
    await caspar.connect()  # 失敗 → False を返すだけ、agent 自体は起動継続
    try:
        await asyncio.gather(
            _subscribe_loop(client, queue, cfg.channel_slug, monitor),
            _dispatch_loop(
                queue,
                caspar,
                cfg.channel_slug,
                cfg,
                monitor,
                channel_media,
                overlay,
                mirrors,
            ),
            _heartbeat_loop(
                client,
                queue,
                caspar,
                cfg.channel_slug,
                cfg.heartbeat_interval_sec,
                monitor,
                cfg.caspar_channel,
                overlay,
            ),
            monitor.run(),
            _prefetch_loop(queue, cache, cfg, channel_media),
            _output_watchdog(queue, caspar, monitor, cfg),
            _recording_upload_retry_loop(recording, cfg),
        )
    finally:
        monitor.stop()
        await caspar.close()
        await client.close()
        queue.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
