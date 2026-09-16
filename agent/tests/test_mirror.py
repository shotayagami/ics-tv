# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MirrorController (exposure_policy #27、docs/site-only-broadcast.md §4.4) の mode 管理を検証。

YT ミラー(公開M/メンバーP)は plan() を経由しない side-channel。mirror_channel 未設定
(None) のノードでは全メソッド no-op になり既存 1ch/2ch ノードを壊さないこと、mode が
queue_db へ永続化され再起動でも復元されることを中心に検証する。
"""

from __future__ import annotations

import asyncio

from icstv_agent.caspar import AmcpResult
from icstv_agent.mirror import MODE_FILLER, MODE_ROUTE, MirrorController
from icstv_agent.queue_db import QueueDb


class _FakeCaspar:
    def __init__(self, *, fail: bool = False) -> None:
        self.commands: list[str] = []
        self._fail = fail

    async def amcp(self, command: str) -> AmcpResult:
        if self._fail:
            raise RuntimeError("amcp send error")
        self.commands.append(command)
        return AmcpResult(code=202, header="OK", body=[])


def _controller(
    tmp_path,
    *,
    seg: str = "yt_public",
    mirror_channel: int | None = 2,
    default_mode: str = MODE_ROUTE,
    filler_clip: str = "filler/site_only_default",
    caspar: _FakeCaspar | None = None,
    queue: QueueDb | None = None,
) -> tuple[MirrorController, QueueDb, _FakeCaspar]:
    db = queue or QueueDb(tmp_path / "q.db")
    cc = caspar or _FakeCaspar()
    controller = MirrorController(
        seg=seg,
        mirror_channel=mirror_channel,
        main_channel=1,
        default_mode=default_mode,
        filler_clip_getter=lambda: filler_clip,
        queue=db,
        caspar=cc,
    )
    return controller, db, cc


def test_disabled_when_mirror_channel_unset(tmp_path):
    """mirror_channel=None (このノードにミラー未設定) なら enabled=False、全メソッド no-op。"""
    controller, _db, caspar = _controller(tmp_path, mirror_channel=None)
    assert controller.enabled is False
    asyncio.run(controller.handle_event("yt_mirror_filler"))
    asyncio.run(controller.reapply())
    assert caspar.commands == []


def test_handle_event_filler_fires_filler_command(tmp_path):
    controller, _db, caspar = _controller(tmp_path, default_mode=MODE_ROUTE)
    asyncio.run(controller.handle_event("yt_mirror_filler"))
    assert caspar.commands == ['PLAY 2-10 "filler/site_only_default" LOOP']


def test_handle_event_route_fires_route_command(tmp_path):
    controller, _db, caspar = _controller(tmp_path, default_mode=MODE_FILLER)
    asyncio.run(controller.handle_event("yt_mirror_route"))
    assert caspar.commands == ["PLAY 2-10 route://1"]


def test_mode_persists_across_restart(tmp_path):
    """agent 再起動 (プロセス再作成) でも直前の mode を queue_db から復元する。"""
    db = QueueDb(tmp_path / "q.db")
    controller, _, _ = _controller(tmp_path, default_mode=MODE_ROUTE, queue=db)
    asyncio.run(controller.handle_event("yt_mirror_filler"))

    # 新しい MirrorController インスタンス (再起動を模す) — 既定は route だが直前の filler を復元
    restarted, _, caspar2 = _controller(tmp_path, default_mode=MODE_ROUTE, queue=db)
    assert restarted._mode == MODE_FILLER
    asyncio.run(restarted.reapply())
    assert caspar2.commands == ['PLAY 2-10 "filler/site_only_default" LOOP']


def test_reapply_resends_current_mode_without_changing_it(tmp_path):
    """reapply() は再接続/起動直後の冪等復元用で、mode 自体は変えない。"""
    controller, _db, caspar = _controller(tmp_path, default_mode=MODE_ROUTE)
    asyncio.run(controller.reapply())
    assert caspar.commands == ["PLAY 2-10 route://1"]
    assert controller._mode == MODE_ROUTE


def test_amcp_failure_is_swallowed(tmp_path):
    """AMCP 発火失敗は例外を外へ伝播させない (本線送出を巻き込まない best-effort)。"""
    caspar = _FakeCaspar(fail=True)
    controller, _db, _ = _controller(tmp_path, default_mode=MODE_ROUTE, caspar=caspar)
    asyncio.run(controller.handle_event("yt_mirror_filler"))  # raise しないことを確認


def test_two_segs_are_independent(tmp_path):
    """公開M/メンバーPは別 seg = 別 state_key で、互いの mode に影響しない。"""
    db = QueueDb(tmp_path / "q.db")
    caspar = _FakeCaspar()
    public, _, _ = _controller(
        tmp_path,
        seg="yt_public",
        mirror_channel=2,
        default_mode=MODE_ROUTE,
        queue=db,
        caspar=caspar,
    )
    members, _, _ = _controller(
        tmp_path,
        seg="yt_members",
        mirror_channel=3,
        default_mode=MODE_FILLER,
        filler_clip="filler/members_default",
        queue=db,
        caspar=caspar,
    )
    asyncio.run(public.handle_event("yt_mirror_filler"))
    asyncio.run(members.handle_event("yt_mirror_route"))
    assert db.get_state("yt_mirror_mode:yt_public") == MODE_FILLER
    assert db.get_state("yt_mirror_mode:yt_members") == MODE_ROUTE
