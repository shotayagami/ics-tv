#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# ICS-TV ライブ静止画 grabber (公開トップ「いま放送中」/ YouTube カード相当)。
#
# MediaMTX が終端した現在ストリームから ~10s おきに 1 フレームを JPEG 化し、公開サイトの
# ingest エンドポイント (管理ホスト/LAN, token=Channel.agent_token) へ POST する。サーバが
# R2 (live/<slug>.jpg) へ上書き保存し、公開トップが短期キャッシュ付きで配信する。
#
# systemd: icstv-poster@<slug> (deploy/playout-node/systemd/icstv-poster@.service)。
# 環境変数: agent-<slug>.env (ICSTV_CHANNEL_SLUG / ICSTV_AGENT_TOKEN) + poster.env (ingest 先)。
#
# 取得元は encoder の UDP 入力ではなく MediaMTX (rtmp://127.0.0.1:1935/hls/<slug>) を使う:
#   MediaMTX が再配信するため encoder leg に影響を与えず独立に取得でき、接続失敗時も自己回復する。
set -u

SLUG="${ICSTV_CHANNEL_SLUG:?ICSTV_CHANNEL_SLUG required (agent-<slug>.env)}"
TOKEN="${ICSTV_AGENT_TOKEN:?ICSTV_AGENT_TOKEN required (agent-<slug>.env)}"
BASE="${ICSTV_POSTER_INGEST_BASE:?ICSTV_POSTER_INGEST_BASE required (poster.env)}"
HOSTHDR="${ICSTV_POSTER_HOST:-}"
INTERVAL="${ICSTV_POSTER_INTERVAL:-10}"
WIDTH="${ICSTV_POSTER_WIDTH:-640}"
SRC="${ICSTV_POSTER_SRC:-rtmp://127.0.0.1:1935/hls/${SLUG}}"

URL="${BASE%/}/internal/live-poster/${SLUG}/"
TMP="$(mktemp --suffix=.jpg)"
trap 'rm -f "$TMP"' EXIT

CURL_HOST=()
[ -n "$HOSTHDR" ] && CURL_HOST=(-H "Host: ${HOSTHDR}")

logger -t icstv-poster "start ch=${SLUG} src=${SRC} -> ${URL} host=${HOSTHDR:-none} every=${INTERVAL}s" 2>/dev/null || true

while true; do
  # MediaMTX から最初の復号可能フレームを 1 枚。timeout で hang を自己回復。
  if timeout 15 ffmpeg -hide_banner -loglevel error -y \
        -rw_timeout 8000000 -analyzeduration 2000000 -probesize 2000000 \
        -i "$SRC" -an -frames:v 1 -vf "scale=${WIDTH}:-2" -q:v 4 -f image2 "$TMP" 2>/dev/null \
     && [ -s "$TMP" ]; then
    # ingress-nginx は HTTP→HTTPS を 308 強制するため BASE は https://<ノードIP>。IP 直叩き +
    # Host ヘッダで振るため証明書 CN は一致しない → -k (内部 LAN の信頼経路。代替: --resolve)。
    curl -fsSk -m 10 -X POST "$URL" \
         "${CURL_HOST[@]}" \
         -H "X-Agent-Token: ${TOKEN}" \
         -H "Content-Type: image/jpeg" \
         --data-binary "@${TMP}" >/dev/null 2>&1 \
      || logger -t icstv-poster "POST failed ch=${SLUG} url=${URL}" 2>/dev/null || true
  else
    logger -t icstv-poster "grab failed ch=${SLUG} src=${SRC}" 2>/dev/null || true
  fi
  sleep "$INTERVAL"
done
