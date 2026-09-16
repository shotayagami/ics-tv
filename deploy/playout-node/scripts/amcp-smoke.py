#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CasparCG AMCP 接続 + CG テンプレ スモークテスト (実機検証準備、casparcg.md §3/§6)。

実 CasparCG (TCP 5250) に対し、接続生存・チャンネル情報・CG テンプレ (提供クレジット) の
ADD/PLAY/UPDATE/INFO/STOP/CLEAR が 2xx で通るかを順に叩く。agent と同じ icstv_agent.caspar を
使うので AMCP のプロトコル解釈 (CRLF・ステータスコード) は本番経路と一致する。

実行 (送出ノード):
  /opt/icstv/agent/.venv/bin/python /opt/icstv/agent/deploy/playout-node/scripts/amcp-smoke.py
  または開発機から: agent/.venv/bin/python deploy/playout-node/scripts/amcp-smoke.py --host <node>

CG レイヤに実際に「提供 スモークテスト提供」が数秒出るので、出力 (CF Live Input / プレビュー) で
目視確認できる。終了コード = 失敗ステップ数。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from icstv_agent.caspar import CasparCgClient


def _quote_json(payload: str) -> str:
    """AMCP data 引数: ダブルクオートで囲み内部の " をエスケープ (casparcg.md §3.3)。"""
    return '"' + payload.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _run(host: str, port: int, channel: int) -> int:
    client = CasparCgClient(host, port)
    if not await client.connect():
        print(
            f"✗ 接続失敗: {host}:{port} (CasparCG 起動 / ファイアウォール / video-mode を確認)"
        )
        return 1

    sponsor1 = _quote_json('{"sponsor":"スモークテスト提供"}')
    sponsor2 = _quote_json('{"sponsor":"UPDATE 動作確認"}')
    # (label, cmd, required)。required=False は非致命 (描画経路の成立に寄与しない補助プローブ)。
    steps: list[tuple[str, str, bool]] = [
        ("バージョン", "VERSION", True),
        ("チャンネル情報", "INFO", True),
        (
            "提供CG ADD(+play)",
            f'CG {channel}-50 ADD 0 "credit/sponsor" 1 {sponsor1}',
            True,
        ),
        ("提供CG UPDATE", f"CG {channel}-50 UPDATE 0 {sponsor2}", True),
        # CG <ch>-<layer> INFO は CasparCG 2.5.0 で [400] を返す版がある (生存確認の補助)。
        # ADD/UPDATE/STOP/CLEAR が通れば CG 描画経路は成立しているので非致命扱い。
        ("提供CG INFO(生存)", f"CG {channel}-50 INFO", False),
        ("提供CG STOP", f"CG {channel}-50 STOP 0", True),
        ("提供CG CLEAR", f"CG {channel}-50 CLEAR", True),
    ]

    failures = 0
    for label, cmd, required in steps:
        try:
            res = await client.amcp(cmd)
            if res.is_success:
                mark = "✓"
            elif required:
                mark = "✗"
                failures += 1
            else:
                mark = "⚠"  # 非致命プローブ
            print(f"{mark} {label:16s} [{res.code}] {cmd}")
            if cmd in ("VERSION", "INFO") and res.body:
                for line in res.body[:4]:
                    print(f"    {line}")
        except Exception as e:  # noqa: BLE001
            if required:
                failures += 1
                print(f"✗ {label:16s} [ERR] {cmd} — {e}")
            else:
                print(f"⚠ {label:16s} [ERR] {cmd} — {e}")
        await asyncio.sleep(0.6)  # CG の出/消しを目視できる程度に間を置く

    await client.close()
    print(
        f"\n結果: {len(steps) - failures}/{len(steps)} OK"
        + ("" if not failures else f" (失敗 {failures})")
    )
    return failures


def main() -> None:
    ap = argparse.ArgumentParser(description="CasparCG AMCP/CG スモークテスト")
    ap.add_argument("--host", default=os.environ.get("ICSTV_CASPAR_HOST", "127.0.0.1"))
    ap.add_argument(
        "--port", type=int, default=int(os.environ.get("ICSTV_CASPAR_PORT", "5250"))
    )
    ap.add_argument(
        "--channel", type=int, default=int(os.environ.get("ICSTV_CASPAR_CHANNEL", "1"))
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.host, args.port, args.channel)))


if __name__ == "__main__":
    main()
