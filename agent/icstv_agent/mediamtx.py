# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MediaMTX HTTP API クライアント (生入力 publisher 監視)。docs/operations.md feed monitor。

agent の依存を増やさない (grpcio のみ) ため、stdlib urllib を asyncio.to_thread で叩く。
MediaMTX は同一ホスト (クラウド GPU ノード) で 127.0.0.1:9997 に API を持つ
(deploy/playout-node/mediamtx/mediamtx.yml)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


class MediaMtxClient:
    def __init__(self, api_url: str) -> None:
        self._base = api_url.rstrip("/")

    async def publisher_ready(self, path: str | None) -> bool | None:
        """指定 path に publisher がいて配信 ready か。

        戻り値:
          True  = publisher あり (ready)
          False = path 不在 or not ready (= feed 断とみなす)
          None  = API 到達不可 (判定保留。誤検知防止のため状態を動かさない)
        path=None のときは「いずれかの path が ready」で判定する。
        """
        try:
            data = await asyncio.to_thread(self._get_paths)
        except Exception:
            logger.debug("MediaMTX API 到達不可", exc_info=True)
            return None
        items = data.get("items", []) if isinstance(data, dict) else []
        if path:
            for it in items:
                if it.get("name") == path:
                    return bool(it.get("ready"))
            return False
        return any(bool(it.get("ready")) for it in items)

    def _get_paths(self) -> dict:
        with urllib.request.urlopen(f"{self._base}/v3/paths/list", timeout=3) as r:
            return json.load(r)

    async def set_record(self, path: str, enabled: bool, record_path: str) -> bool:
        """path の録画 on/off を切替える (生放送録画 record_live 連携)。

        MediaMTX ランタイム config API (POST /v3/config/paths/patch/<path>) に
        {"record": enabled, "recordPath": record_path} を送る。best-effort: 失敗しても例外は
        投げず False を返すのみ (呼び出し元は本線送出 (AMCP) を止めてはならないため、これを
        try/except で包んでさらに握り潰す)。
        """
        try:
            await asyncio.to_thread(self._patch_record, path, enabled, record_path)
            return True
        except Exception:
            logger.warning("MediaMTX 録画切替失敗 path=%s enabled=%s", path, enabled, exc_info=True)
            return False

    def _patch_record(self, path: str, enabled: bool, record_path: str) -> None:
        body = json.dumps({"record": enabled, "recordPath": record_path}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}/v3/config/paths/patch/{path}",
            data=body,
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3):
            pass
