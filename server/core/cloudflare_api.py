# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cloudflare Stream Live Input / Live Output の薄いラッパー (httpx)。

役割 (docs/overview.md 3.6, docs/youtube.md):
- create_live_input(channel): RTMP 受信口 (我々の CasparCG 出力先) を作成し channel に保存
- create_live_output(channel, url, stream_key): YouTube ingest への再送出 Output を追加
- update_live_output(channel, output_uid, enabled): 既存 Output の enabled を切替 (#27 Part B。
  クリエイター個人チャンネル宛シミュルキャストの窓制御に使う。CF は enabled のみの部分更新に対応)
- delete_live_output(channel, output_uid): Output を削除
- delete_live_input (Phase 2、未実装)

env:
  ICSTV_CF_API_TOKEN  (Stream:Edit 権限)
  ICSTV_CF_ACCOUNT_ID (Account ID)

エラーは httpx.HTTPStatusError を素のまま投げる。view 側で 502 等にマッピング。
"""

from __future__ import annotations

import os

import httpx

from core.models import Channel

_BASE = "https://api.cloudflare.com/client/v4"
_TIMEOUT = httpx.Timeout(10.0)


class CloudflareNotConfiguredError(RuntimeError):
    pass


def _client() -> httpx.Client:
    token = os.environ.get("ICSTV_CF_API_TOKEN")
    if not token:
        raise CloudflareNotConfiguredError("ICSTV_CF_API_TOKEN が未設定")
    return httpx.Client(
        base_url=_BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )


def _account_id() -> str:
    aid = os.environ.get("ICSTV_CF_ACCOUNT_ID")
    if not aid:
        raise CloudflareNotConfiguredError("ICSTV_CF_ACCOUNT_ID が未設定")
    return aid


def create_live_input(channel: Channel) -> dict:
    """Live Input (RTMP 受信口) を作成し channel.cf_live_input_id に保存。"""
    if channel.cf_live_input_id:
        raise ValueError(
            f"channel {channel.slug} already has cf_live_input_id={channel.cf_live_input_id}"
        )
    body = {
        "meta": {"name": f"ICS-TV {channel.slug} ingest"},
        "recording": {"mode": "off"},  # CF 録画は使わない (R2 で別管理)
    }
    with _client() as c:
        r = c.post(f"/accounts/{_account_id()}/stream/live_inputs", json=body)
        r.raise_for_status()
    result = r.json().get("result", {})
    channel.cf_live_input_id = result.get("uid", "")
    channel.save(update_fields=["cf_live_input_id"])
    return result


def create_live_output(channel: Channel, *, target_url: str, stream_key: str) -> dict:
    """Live Output を追加し、当該 Live Input が target (YouTube ingest 等) へ転送するようにする。

    target_url 例: rtmp://a.rtmp.youtube.com/live2
    stream_key:    YouTube 永続キー (channel.youtube_stream_key)
    """
    if not channel.cf_live_input_id:
        raise ValueError(f"channel {channel.slug} has no cf_live_input_id (Live Input を先に作成)")
    body = {
        "url": target_url,
        "streamKey": stream_key,
        "enabled": True,
    }
    path = f"/accounts/{_account_id()}/stream/live_inputs/{channel.cf_live_input_id}/outputs"
    with _client() as c:
        r = c.post(path, json=body)
        r.raise_for_status()
    return r.json().get("result", {})


def list_live_outputs(channel: Channel) -> list[dict]:
    if not channel.cf_live_input_id:
        return []
    path = f"/accounts/{_account_id()}/stream/live_inputs/{channel.cf_live_input_id}/outputs"
    with _client() as c:
        r = c.get(path)
        r.raise_for_status()
    return r.json().get("result", [])


def update_live_output(channel: Channel, output_uid: str, *, enabled: bool) -> dict:
    """既存 Output の enabled を切替える (#27 Part B: クリエイター個人チャンネル宛シミュルキャストの
    窓制御。番組の on-air/off-air に合わせて reconciler がこれを呼ぶ)。CF API は enabled のみの
    部分更新に対応 (url/streamKey の再送は不要)。
    """
    if not channel.cf_live_input_id:
        raise ValueError(f"channel {channel.slug} has no cf_live_input_id")
    path = (
        f"/accounts/{_account_id()}/stream/live_inputs/{channel.cf_live_input_id}"
        f"/outputs/{output_uid}"
    )
    with _client() as c:
        r = c.put(path, json={"enabled": enabled})
        r.raise_for_status()
    return r.json().get("result", {})


def delete_live_output(channel: Channel, output_uid: str) -> None:
    if not channel.cf_live_input_id:
        raise ValueError(f"channel {channel.slug} has no cf_live_input_id")
    path = (
        f"/accounts/{_account_id()}/stream/live_inputs/{channel.cf_live_input_id}"
        f"/outputs/{output_uid}"
    )
    with _client() as c:
        r = c.delete(path)
        r.raise_for_status()
