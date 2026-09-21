# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lバー(1-30) / 次番組予告(1-40) の常駐オーバーレイ状態管理。

本線(layer 10)とは別レイヤーで、状態 (常駐済か / 現タイトル / CM退避 / 残り3分タイマー) を持つ
ため plan() の stateless な cg 列とは分離し、ここで ADD/UPDATE/PLAY/STOP を調停する。

責務:
- Lバー: 本線コンテンツ (asset/filler/live) 中は常時表示。タイトルは UPDATE で差し替え (再ADDせず
  チラつき防止)。CM/スレート中は STOP で退避。
- 次番組予告: フィラー中は常時、番組枠は残り3分 (cg_preview_at) から終了まで、CM中は退避。
- CasparCG 再起動 (retake) 時は restore() で再 ADD して復帰。

全コマンドは best-effort (本線には影響しないので例外は握り潰す)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from icstv.v1 import playout_pb2

from icstv_agent import amcp_planner

logger = logging.getLogger(__name__)

# 本線コンテンツ = Lバーを出す action。CM/スレートは退避。
# PLAY_VT (生番組内 VT ロール) も番組継続中の一部として扱う (CM のような退避対象ではない)。
# 直前 CUT_LIVE の cg_* params をそのまま引き継ぐ前提 (server 側 fire_vt_asset、#25 §6.7)。
_CONTENT_ACTIONS = frozenset(
    {
        playout_pb2.PLAYOUT_ACTION_PLAY_ASSET,
        playout_pb2.PLAYOUT_ACTION_PLAY_FILLER,
        playout_pb2.PLAYOUT_ACTION_CUT_LIVE,
        playout_pb2.PLAYOUT_ACTION_PLAY_VT,
    }
)
_HIDE_ACTIONS = frozenset(
    {
        playout_pb2.PLAYOUT_ACTION_PLAY_CM,
        playout_pb2.PLAYOUT_ACTION_PLAY_CM_BUNDLE,
        playout_pb2.PLAYOUT_ACTION_PLAY_SLATE,
    }
)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class OverlayManager:
    def __init__(
        self,
        caspar,
        *,
        channel: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._caspar = caspar
        self._channel = channel
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lbar_added = False
        self._lbar_visible = False
        self._lbar_title: str | None = None
        self._preview_added = False
        self._preview_visible = False
        self._preview_data: tuple[str, str] | None = None
        self._preview_timer: asyncio.Task | None = None
        # 自動グラフィックセット (cg_cues, #18 §C): layer→現表示シグネチャ、タイマー群。
        self._cue_state: dict[int, str] = {}
        self._cue_timers: list[asyncio.Task] = []
        # 朝夕の左上時計 (layer36) が現在表示中か。表示中は Lバー時計を消し予告を右へ寄せる (適応動作)。
        self._clock_active = False

    def snapshot(self) -> dict[int, dict]:
        """OverlayManager 管理レイヤ(Lバー30/予告35)の content/visible スナップショット (#18 §A)。

        INFO だけでは CG の表示状態(CSS)まで取れないため、内部状態を一次情報として heartbeat に載せる。
        """
        snap: dict[int, dict] = {}
        if self._lbar_added:
            snap[amcp_planner.LAYER_LBAR] = {
                "content": self._lbar_title or "",
                "visible": self._lbar_visible,
            }
        if self._preview_added:
            snap[amcp_planner.LAYER_PREVIEW] = {
                "content": self._preview_data[0] if self._preview_data else "",
                "visible": self._preview_visible,
            }
        return snap

    async def on_event(self, action: int, params: dict[str, str]) -> None:
        """take 成功後に呼ぶ。action と cg_* params からオーバーレイを調停する。"""
        if action == playout_pb2.PLAYOUT_ACTION_OVERLAY_OP:
            # 手動 op が触れたレイヤは自動 cue の内部状態を無効化し、次の本線境界で
            # _apply_cues に取り戻させる (手動 clear/show と自動 cue が同一レイヤを共有する場合の整合)。
            try:
                layer = int(params.get("overlay_layer") or 0)
            except ValueError:
                layer = 0
            self._cue_state.pop(layer, None)
            return
        self._cancel_timer()  # 新 take は前イベントの予告タイマーを無効化
        if action in _HIDE_ACTIONS:
            await self._hide_lbar()
            await self._hide_preview()
            await self._clear_all_cues()  # CM/スレート中は自動グラフィックも退避
            return
        if action == playout_pb2.PLAYOUT_ACTION_CLEAR_SLATE:
            # スレート解除 → 本線が前面に戻る。Lバーを最後のタイトルで復帰 (予告は次の本線で再評価)。
            if self._lbar_added:
                await self._send(amcp_planner.lbar_play_cmd(self._channel))
            return
        if action not in _CONTENT_ACTIONS:
            return  # yt_transition 等 (本来 take されないが念のため)

        if params.get("cg_lbar_hidden"):
            await self._hide_lbar()
        else:
            await self._show_lbar(params.get("cg_title", ""), params.get("cg_channel") or "ICS-TV")

        if params.get("cg_preview_show"):  # フィラー: 常時表示
            await self._show_preview(
                params.get("cg_next_title", ""), params.get("cg_next_start", "")
            )
        elif params.get("cg_preview_at"):  # 番組枠: 残り3分 (cg_preview_at) から表示
            await self._hide_preview()
            self._schedule_preview(
                params["cg_preview_at"],
                params.get("cg_next_title", ""),
                params.get("cg_next_start", ""),
            )
        else:  # 次番組なし等 → 予告は出さない
            await self._hide_preview()

        await self._apply_cues(params.get("cg_cues", ""))  # 自動グラフィックセット (#18 §C)

    async def restore(self, action: int, params: dict[str, str]) -> None:
        """CasparCG 再起動後の retake 時: CG は消えているので再 ADD させる。"""
        self._cancel_timer()
        self._lbar_added = False
        self._lbar_visible = False
        self._lbar_title = None
        self._preview_added = False
        self._preview_visible = False
        self._preview_data = None
        self._cancel_cue_timers()
        self._cue_state = {}  # CG 消失済 → 次の on_event で再 ADD させる
        self._clock_active = False  # 朝夕時計も消失 → 次の _apply_cues で再判定
        await self.on_event(action, params)

    # ---- 自動グラフィックセット (cg_cues, #18 §C) ----

    async def _apply_cues(self, cues_json: str) -> None:
        """cg_cues (絶対 show_at/hide_at の配列 JSON) を調停。予告タイマー機構の一般化。

        - 表示中で今回の集合に無いレイヤは CLEAR (番組→フィラー遷移等)。
        - now が窓内のキューは即 show (シグネチャ不変なら再 ADD せずチラつき防止) + hide タイマー。
        - 未来のキューは show タイマー。過去のキューは出さない。
        """
        self._cancel_cue_timers()
        try:
            cues = json.loads(cues_json) if cues_json else []
        except ValueError:
            cues = []
        now = self._clock()
        desired = {int(c["layer"]) for c in cues}
        for layer in list(self._cue_state):
            if layer not in desired:
                await self._clear_cue_layer(layer)
        for cue in cues:
            show_at = _parse_dt(cue.get("show_at"))
            hide_at = _parse_dt(cue.get("hide_at"))
            if show_at is None:
                continue
            if hide_at is not None and now >= hide_at:
                await self._clear_cue_layer(int(cue["layer"]))  # 窓を過ぎた
                continue
            if show_at <= now:
                await self._show_cue(cue)
                if hide_at is not None:
                    self._schedule_cue(hide_at - now, self._clear_cue_layer, int(cue["layer"]))
            else:
                self._schedule_cue(show_at - now, self._show_cue_then_hide, cue)

    async def _show_cue_then_hide(self, cue: dict) -> None:
        await self._show_cue(cue)
        hide_at = _parse_dt(cue.get("hide_at"))
        if hide_at is not None:  # _schedule_cue は delay<=0 で即時発火
            self._schedule_cue(hide_at - self._clock(), self._clear_cue_layer, int(cue["layer"]))

    async def _show_cue(self, cue: dict) -> None:
        layer = int(cue["layer"])
        sig = (
            cue.get("kind", "graphic")
            + ":"
            + json.dumps(cue.get("data", {}), sort_keys=True, ensure_ascii=False)
        )
        if self._cue_state.get(layer) == sig:
            return  # 既に同内容表示中 → 再 ADD せず
        params = {
            "overlay_layer": str(layer),
            "overlay_op": "show",
            "overlay_kind": cue.get("kind", "graphic"),
            "overlay_template": cue.get("template", "") or "",
            "overlay_data": json.dumps(cue.get("data", {}), ensure_ascii=False),
        }
        if cue.get("kind") == "video":
            d = cue.get("data", {})
            params["overlay_clip"] = d.get("clip", "")
            if d.get("loop"):
                params["overlay_loop"] = "1"
        await self._send(amcp_planner.overlay_op_cmd(self._channel, params))
        self._cue_state[layer] = sig
        if layer == amcp_planner.LAYER_CLOCK:  # 朝夕時計が出た → Lバー時計OFF + 予告右寄せ
            await self._set_clock_active(True)

    async def _clear_cue_layer(self, layer: int) -> None:
        if layer in self._cue_state:
            await self._send(f"CLEAR {self._channel}-{layer}")
            del self._cue_state[layer]
            if layer == amcp_planner.LAYER_CLOCK:  # 朝夕時計が消えた → Lバー時計/予告を復帰
                await self._set_clock_active(False)

    async def _clear_all_cues(self) -> None:
        self._cancel_cue_timers()
        for layer in list(self._cue_state):
            await self._clear_cue_layer(layer)

    def _cancel_cue_timers(self) -> None:
        for t in self._cue_timers:
            if not t.done():
                t.cancel()
        self._cue_timers = []

    def _schedule_cue(self, delay, coro_fn, arg) -> None:
        secs = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)

        async def _fire() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                if secs > 0:
                    await asyncio.sleep(secs)
                await coro_fn(arg)

        self._cue_timers.append(asyncio.create_task(_fire()))

    # ---- 内部 ----

    async def _send(self, cmd: str) -> None:
        try:
            await self._caspar.amcp(cmd)
        except Exception:
            logger.warning("overlay CG 失敗 (best-effort): %s", cmd)

    async def _set_clock_active(self, active: bool) -> None:
        """朝夕時計(layer36)の表示/非表示に追従して Lバー時計と予告オフセットを切替える (適応動作)。

        時計帯=Lバー側の時計を消し(左上時計に一本化) + 予告を右へ寄せて時計を避ける。
        窓外=どちらも従来表示へ復帰。Lバー/予告が未 ADD の間は次回 _show_* が現状態で出すため何もしない。
        """
        if self._clock_active == active:
            return
        self._clock_active = active
        if self._lbar_added:
            await self._send(amcp_planner.lbar_update_cmd(self._channel, clock=not active))
        if self._preview_added:
            await self._send(amcp_planner.preview_avoid_cmd(self._channel, avoid=active))

    async def _show_lbar(self, title: str, channel_name: str) -> None:
        if not self._lbar_added:
            await self._send(
                amcp_planner.lbar_add_cmd(
                    self._channel,
                    title=title,
                    channel_name=channel_name,
                    clock=not self._clock_active,  # 時計帯は Lバー時計を出さない
                )
            )
            self._lbar_added = True
            self._lbar_title = title
        elif title != self._lbar_title:
            await self._send(amcp_planner.lbar_update_cmd(self._channel, title=title))
            self._lbar_title = title
        await self._send(amcp_planner.lbar_play_cmd(self._channel))  # CM退避からの復帰 (idempotent)
        self._lbar_visible = True

    async def _hide_lbar(self) -> None:
        if self._lbar_added:
            await self._send(amcp_planner.lbar_stop_cmd(self._channel))
        self._lbar_visible = False

    async def _show_preview(self, title: str, start: str) -> None:
        if not self._preview_added:
            await self._send(
                amcp_planner.preview_add_cmd(
                    self._channel,
                    title=title,
                    start=start,
                    avoid_clock=self._clock_active,  # 時計帯は予告を右へ寄せて時計を避ける
                )
            )
            self._preview_added = True
            self._preview_data = (title, start)
        elif (title, start) != self._preview_data:
            await self._send(
                amcp_planner.preview_update_cmd(self._channel, title=title, start=start)
            )
            self._preview_data = (title, start)
        await self._send(amcp_planner.preview_play_cmd(self._channel))
        self._preview_visible = True

    async def _hide_preview(self) -> None:
        if self._preview_added:
            await self._send(amcp_planner.preview_stop_cmd(self._channel))
        self._preview_visible = False

    def _cancel_timer(self) -> None:
        if self._preview_timer is not None and not self._preview_timer.done():
            self._preview_timer.cancel()
        self._preview_timer = None

    def _schedule_preview(self, at_iso: str, title: str, start: str) -> None:
        """番組終了-180s (at_iso) に予告を出すタイマーを張る。既に過ぎていれば即時表示。"""
        try:
            at = datetime.fromisoformat(at_iso)
        except ValueError:
            return
        delay = (at - self._clock()).total_seconds()

        async def _fire() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._show_preview(title, start)

        self._preview_timer = asyncio.create_task(_fire())
