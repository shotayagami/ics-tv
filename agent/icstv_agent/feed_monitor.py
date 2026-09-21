# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""feed monitor (docs/operations.md O2/O6)。

生入力 (MediaMTX publisher) を周期監視し:
- 断 (publisher 消失を lost_ticks 連続) → goto_slate で自動スレート退避 + ReportInterrupt(SLATE_ON)
- 復帰 (publisher 再出現を hysteresis_ticks 連続) → 生を本線へ戻し CLEAR slate + ReportInterrupt(FEED_RESTORED)
- フラップ制限: flap_window 内の自動復帰が flap_max を超えたら自動復帰をサスペンド
  (operator トグル auto_return とは別の内部状態) + ReportInterrupt(AUTO_RETURN_SUSPENDED)
- 手動/緊急スレート (monitor 由来でない) が出ている間は自動復帰しない (調停)

判定は「live 区間」(cut_live 実行中) のみ。録画/フィラー中は idle で判定しない。
I/O (caspar.amcp / mediamtx.publisher_ready / send_interrupt) は注入し状態機械を単体テスト可能に。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from icstv.v1 import playout_pb2

from icstv_agent import amcp_planner
from icstv_agent.channel_media import ChannelMedia

logger = logging.getLogger(__name__)

LOST = "lost"
OK = "ok"
IDLE = "idle"

_KINDS = playout_pb2.ReportInterruptRequest.InterruptKind


def _path_from_rtmp(url: str | None) -> str | None:
    """rtmp://host:port/live/ch1 → 'live/ch1' (MediaMTX path 名)。"""
    if not url:
        return None
    return urlparse(url).path.lstrip("/") or None


def _rtmp_from_params(params: dict[str, str]) -> str | None:
    url = params.get("rtmp_url")
    if url:
        return url
    app = params.get("rtmp_app")
    key = params.get("rtmp_key")
    if app and key:
        return f"rtmp://127.0.0.1:1935/{app}/{key}"
    return None


class FeedMonitor:
    def __init__(
        self,
        *,
        caspar,
        mediamtx,
        send_interrupt,  # async (kind:int, detail:str) -> None
        channel: int,
        channel_media: ChannelMedia,
        channel_slug: str = "",  # RecordingManager へ渡す ingest キー組み立て用 (record_live)
        poll_sec: float = 1.0,
        lost_ticks: int = 2,
        hysteresis_ticks: int = 10,
        flap_window_sec: int = 600,
        flap_max: int = 3,
        auto_return: bool = True,
        clock=None,
        recording=None,  # RecordingManager | None (生放送録画 record_live 連携)
    ) -> None:
        self._caspar = caspar
        self._mediamtx = mediamtx
        self._send_interrupt = send_interrupt
        self._channel = channel
        self._channel_media = channel_media
        self._channel_slug = channel_slug
        self._poll_sec = poll_sec
        self._lost_ticks = lost_ticks
        self._hysteresis_ticks = hysteresis_ticks
        self._flap_window = timedelta(seconds=flap_window_sec)
        self._flap_max = flap_max
        self._clock = clock or (lambda: datetime.now(UTC))
        self._recording = recording
        # 録画 on/off は非同期 (MediaMTX API 呼び出し) だが on_event_taken は同期呼び出しのため、
        # background task として起動する。参照を保持しないと GC される可能性があるため退避する。
        self._recording_tasks: set[asyncio.Task] = set()
        # ---- 状態 ----
        self.live_active = False
        self.live_rtmp_url: str | None = None
        self.live_path: str | None = None
        self.recording_active = False  # 現在 live_path を録画中か (record_live)
        self.slate_active = False
        self.slate_by_monitor = False  # 今のスレートは monitor (feed断) が出したものか
        self.manual_slate_active = False  # 手動/緊急スレート中 (自動復帰サスペンド条件)
        self.auto_return = auto_return  # operator トグルの意図
        self.auto_return_suspended = False  # フラップ保護による一時サスペンド
        self.feed_state = IDLE
        self._lost = 0
        self._ok = 0
        self._returns: deque[datetime] = deque()
        self._running = True

    # ---- dispatch loop からの通知 ----

    def on_event_taken(self, action: int, params: dict[str, str]) -> None:
        a = playout_pb2
        if action == a.PLAYOUT_ACTION_CUT_LIVE:
            self.live_active = True
            self.live_rtmp_url = _rtmp_from_params(params)
            self.live_path = _path_from_rtmp(self.live_rtmp_url)
            self.feed_state = OK
            self._lost = self._ok = 0
            # 生放送録画 (record_live)。CUT_LIVE の AMCP 発行を一切阻害してはならないため、
            # MediaMTX API 呼び出しは background task で fire-and-forget する (例外は握り潰す)。
            if params.get("record") == "1" and self.live_path:
                program_id = params.get("program_id")
                self._spawn_recording(self._start_recording(self.live_path, program_id))
        elif action in (a.PLAYOUT_ACTION_PLAY_ASSET, a.PLAYOUT_ACTION_PLAY_FILLER):
            # 生区間終了。録画中なら停止+アップロードを background task で発火する
            # (これも本線の PLAY_ASSET/PLAY_FILLER dispatch を絶対に阻害しない)。
            if self.recording_active and self.live_path:
                self._spawn_recording(self._stop_recording(self.live_path))
            self.live_active = False
            self.feed_state = IDLE
            self._lost = self._ok = 0
        elif action == a.PLAYOUT_ACTION_PLAY_SLATE:
            # 手動/緊急スレート → 自動復帰を抑止する調停 (operator 意図でスレート継続)。
            # monitor が既に退避中でも、手動が被さったら manual フラグで自動復帰を止める。
            self.manual_slate_active = True
            self.slate_active = True
        elif action == a.PLAYOUT_ACTION_CLEAR_SLATE:
            self.manual_slate_active = False
            self.slate_active = False
            self.slate_by_monitor = False
        # play_cm_bundle は live_active を変えない (CM IN 中も live 区間継続)

    def on_agent_control(self, auto_return: bool) -> None:
        """server からの AgentControl (自動復帰トグル)。ON 再送はサスペンドも解除する。"""
        self.auto_return = auto_return
        if auto_return:
            self.auto_return_suspended = False

    # ---- 監視 tick ----

    async def tick(self) -> None:
        if not self.live_active:
            self.feed_state = IDLE
            self._lost = self._ok = 0
            return
        ready = await self._mediamtx.publisher_ready(self.live_path)
        if ready is None:
            return  # API 到達不可 = 判定保留 (誤検知防止)
        now = self._clock()
        if ready:
            self._ok += 1
            self._lost = 0
            if self.slate_active and self.slate_by_monitor:
                if (
                    self.auto_return
                    and not self.auto_return_suspended
                    and not self.manual_slate_active
                    and self._ok >= self._hysteresis_ticks
                ):
                    await self._return_to_live()
                    self.slate_active = False
                    self.slate_by_monitor = False
                    self.feed_state = OK
                    self._ok = 0
                    self._record_return(now)
                    await self._send_interrupt(_KINDS.INTERRUPT_KIND_FEED_RESTORED, "")
                    if self.auto_return_suspended:
                        await self._send_interrupt(
                            _KINDS.INTERRUPT_KIND_AUTO_RETURN_SUSPENDED,
                            "flap 上限超過",
                        )
            else:
                self.feed_state = OK
        else:
            self._lost += 1
            self._ok = 0
            if not self.slate_active and self._lost >= self._lost_ticks:
                with contextlib.suppress(Exception):
                    await self._caspar.amcp(
                        amcp_planner.slate_command(self._channel, self._channel_media.slate_clip)
                    )
                self.slate_active = True
                self.slate_by_monitor = True
                self.feed_state = LOST
                await self._send_interrupt(
                    _KINDS.INTERRUPT_KIND_SLATE_ON,
                    f"feed 断 (publisher lost): {self.live_path}",
                )

    async def _return_to_live(self) -> None:
        ch = self._channel
        main = f"{ch}-{amcp_planner.LAYER_MAIN}"
        rtmp = self.live_rtmp_url or ""
        # LOADBG → INFO 確認 (best-effort) → PLAY → CLEAR slate (docs/casparcg.md §4.5)
        with contextlib.suppress(Exception):
            await self._caspar.amcp(f'LOADBG {main} "{rtmp}" MIX 15')
            with contextlib.suppress(Exception):
                await self._caspar.amcp(f"INFO {main}")
            await self._caspar.amcp(f"PLAY {main}")
            await self._caspar.amcp(amcp_planner.clear_slate_command(ch))

    def _record_return(self, now: datetime) -> None:
        self._returns.append(now)
        cutoff = now - self._flap_window
        while self._returns and self._returns[0] < cutoff:
            self._returns.popleft()
        if len(self._returns) > self._flap_max:
            self.auto_return_suspended = True

    # ---- 生放送録画 (record_live) ----
    #
    # 録画 on/off + アップロードは RecordingManager (io を注入した独立モジュール) に委譲する。
    # ここでは「本線送出を絶対に止めない」ための background task 発火のみを担う。
    # asyncio.create_task の例外は task 自体に留まり呼び出し元へ伝播しないが、ログに残らず
    # サイレントに消える可能性があるため _guarded で必ず try/except しログする。

    def _spawn_recording(self, coro) -> None:
        task = asyncio.create_task(self._guarded(coro))
        self._recording_tasks.add(task)
        task.add_done_callback(self._recording_tasks.discard)

    async def _guarded(self, coro) -> None:
        try:
            await coro
        except Exception:
            # 録画/アップロード関連の失敗は best-effort。本線送出 (AMCP) には一切影響させない
            # (計画のリスク低減策で最重要事項)。
            logger.warning("録画処理でエラー (best-effort、送出には影響なし)", exc_info=True)

    async def _start_recording(self, path: str, program_id: str | None) -> None:
        if self._recording is None or not program_id:
            return
        await self._recording.start(path, self._channel_slug, int(program_id))
        self.recording_active = True

    async def _stop_recording(self, path: str) -> None:
        if self._recording is None:
            return
        await self._recording.stop_and_upload(path)
        self.recording_active = False

    async def run(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("feed monitor tick error")
            await asyncio.sleep(self._poll_sec)

    def stop(self) -> None:
        self._running = False
