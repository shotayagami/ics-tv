#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# Proxmox ホスト (ssh root@192.0.2.12) へ SSH して実行する (旧記述の管理用 VM は 2026-08-11 停止済)。
# 送出ノード LXC を作成し iGPU を共有する。CTID は必須 (既定値なし。未設定ならエラー終了) —
# 新規作成時は既存の CT と衝突しない値を CTID=NNN で指定する。
# 冪等: 既に存在する CTID には create をスキップし set のみ行う。
#
# 使い方:  CTID=NNN deploy/playout-node/scripts/provision-lxc.sh
# 前提:    Proxmox ホスト上に pct/pvesh、テンプレートが local に DL 済み。
set -euo pipefail

CTID="${CTID:?CTID は必須です (作成する CT の CTID を CTID=NNN で指定する)}"
# HOSTNAME は bash が起動時に自動設定するシェル変数なので、既定値を与えても効かない
# (明示しないと Proxmox ホスト自身の名前が CT に付く)。専用の変数名にしている。
CT_HOSTNAME="${CT_HOSTNAME:-icstv-playout}"
TEMPLATE="${TEMPLATE:-local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
ROOTFS="${ROOTFS:-local-lvm:48}"
# media cache (prefetch LRU) 用。MEDIA_STORE="" でスキップ可 (Gate1-7 検証や、当該ノードに
# 専用ディスクが無い場合は media を rootfs に置く)。
# 注: :- でなく - (空文字を尊重)。MEDIA_STORE="" は「mp0 スキップ」の明示指定として扱う。
MEDIA_STORE="${MEDIA_STORE-local-lvm}"
MEDIA_SIZE="${MEDIA_SIZE:-100}"
# IP_CIDR/GW の既定値は RFC 5737 TEST-NET-1 の例示 (実 LAN には存在しない)。
# 必ず IP_CIDR/GW を自分の LAN の値で指定して実行すること。
IP_CIDR="${IP_CIDR:-192.0.2.20/24}"
GW="${GW:-192.0.2.1}"
ONBOOT="${ONBOOT:-1}"

if ! pct status "$CTID" >/dev/null 2>&1; then
  echo "==> create CT $CTID ($CT_HOSTNAME)"
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores 3 --cpulimit 3 --cpuunits 50 \
    --memory 4096 --swap 2048 \
    --rootfs "$ROOTFS" \
    --net0 "name=eth0,bridge=vmbr0,ip=${IP_CIDR},gw=${GW},firewall=1" \
    --features nesting=1 --onboot "$ONBOOT" --unprivileged 0 --start 0
else
  echo "==> CT $CTID already exists, skip create"
fi

# 素材キャッシュ (rootfs と別ボリューム。既定の local-lvm は rootfs の既定と同じストレージなので、k8s のコントロールプレーンノードの etcd や k8s のワーカーノードが使うディスクを避けるには MEDIA_STORE に別ディスクのストレージを指定する)。
# MEDIA_STORE="" のときは mp0 を作らず media を rootfs に置く (専用ディスク無しノード向け)。
if [ -n "$MEDIA_STORE" ]; then
  if ! pct config "$CTID" | grep -q '^mp0:'; then
    echo "==> add media cache mp0 (${MEDIA_STORE}:${MEDIA_SIZE} -> /opt/casparcg/media)"
    pct set "$CTID" -mp0 "${MEDIA_STORE}:${MEDIA_SIZE},mp=/opt/casparcg/media"
  fi
else
  echo "==> MEDIA_STORE 未指定: media cache mp0 をスキップ (media は rootfs)"
fi

echo "==> start CT $CTID (gid 実測のため先に起動)"
pct start "$CTID" 2>/dev/null || true
for _ in $(seq 1 15); do pct exec "$CTID" -- true 2>/dev/null && break; sleep 1; done

# iGPU 共有: gid は **CT 内** の render/video を使う (privileged CT は host と 1:1。Ubuntu24.04 で render=993/video=44)
RENDER_GID="$(pct exec "$CTID" -- getent group render | cut -d: -f3)"
VIDEO_GID="$(pct exec "$CTID" -- getent group video | cut -d: -f3)"
echo "==> CT render gid=$RENDER_GID video gid=$VIDEO_GID"
# card デバイスはホストにより card0/card1 と異なる → ホスト側で自動検出。
# renderD128 は標準で固定。
CARD_DEV="$(ls /dev/dri/card* 2>/dev/null | sort | head -1)"
echo "==> iGPU card device = ${CARD_DEV:-/dev/dri/card0}"
pct set "$CTID" -dev0 "/dev/dri/renderD128,gid=${RENDER_GID}"
pct set "$CTID" -dev1 "${CARD_DEV:-/dev/dri/card0},gid=${VIDEO_GID}"
echo "==> reboot CT $CTID (dev 反映)"
pct reboot "$CTID" 2>/dev/null || true
echo
echo "次: CT 内で配備 (リポジトリを clone してから install.sh):"
echo "  pct exec $CTID -- bash -lc 'apt-get update && apt-get install -y git ca-certificates'"
echo "  pct exec $CTID -- git clone https://<git-host>/<your-org>/icstv.git /opt/icstv"
echo "  pct exec $CTID -- bash /opt/icstv/deploy/playout-node/scripts/install.sh"
