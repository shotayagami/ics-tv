# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""exposure_policy (#27、docs/site-only-broadcast.md §4.4) の YTミラー制御。

公開ミラー M / メンバーミラー P は plan() を経由せず、main.py の _do_loadbg/_do_take が
action 判定して直接 MirrorController へ委譲する (slate/yt_transition と同じ側道流儀)。
seg 別(yt_public/yt_members)に1インスタンスずつ生成し、既定 mode がミラーごとに異なる
(公開M既定=route、メンバーP既定=filler)。mirror_channel が None (このノードにミラー未設定)
なら全メソッド no-op になり、既存 1ch/2ch ノードを壊さない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from icstv_agent import amcp_planner
from icstv_agent.caspar import CasparCgClient
from icstv_agent.queue_db import QueueDb

logger = logging.getLogger(__name__)

MODE_ROUTE = "route"
MODE_FILLER = "filler"


class MirrorController:
    """1 つの YT ミラー (seg 別、公開 M またはメンバー P) の mode 管理。"""

    def __init__(
        self,
        *,
        seg: str,
        mirror_channel: int | None,
        main_channel: int,
        default_mode: str,
        filler_clip_getter: Callable[[], str],
        queue: QueueDb,
        caspar: CasparCgClient,
    ) -> None:
        self.seg = seg
        self.mirror_channel = mirror_channel
        self.main_channel = main_channel
        self.default_mode = default_mode
        self._filler_clip_getter = filler_clip_getter
        self._queue = queue
        self._caspar = caspar
        self._mode = self._restore_mode()

    @property
    def enabled(self) -> bool:
        return self.mirror_channel is not None

    def _state_key(self) -> str:
        return f"yt_mirror_mode:{self.seg}"

    def _restore_mode(self) -> str:
        """queue_db から永続化済み mode を復元する (再起動でも公開YouTubeに本編が漏れない/
        メンバー枠が外れないようにするため)。未設定/不正値なら既定 mode。"""
        if not self.enabled:
            return self.default_mode
        saved = self._queue.get_state(self._state_key())
        return saved if saved in (MODE_ROUTE, MODE_FILLER) else self.default_mode

    async def handle_event(self, action: str) -> None:
        """YT_MIRROR_FILLER/YT_MIRROR_ROUTE の action(文字列)を受けて mode 更新+AMCP発火する。"""
        if not self.enabled:
            return
        mode = MODE_FILLER if action == "yt_mirror_filler" else MODE_ROUTE
        self._mode = mode
        self._queue.set_state(self._state_key(), mode)
        await self._fire()

    async def reapply(self) -> None:
        """現在の mode を再送する (CasparCG 再接続/agent 起動直後の冪等復元)。"""
        if not self.enabled:
            return
        await self._fire()

    async def _fire(self) -> None:
        assert self.mirror_channel is not None
        if self._mode == MODE_FILLER:
            cmd = amcp_planner.yt_mirror_filler_command(
                self.mirror_channel, self._filler_clip_getter()
            )
        else:
            cmd = amcp_planner.yt_mirror_route_command(self.mirror_channel, self.main_channel)
        try:
            await self._caspar.amcp(cmd)
        except Exception:
            logger.warning("yt mirror(%s) AMCP発火失敗: %s", self.seg, cmd)
