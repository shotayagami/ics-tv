#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 送出ノード LXC の中で root 実行。VAAPI/CasparCG/MediaMTX/icstv-agent を導入し systemd 配備。
# 2026-06-10 に実証用 LXC (Ubuntu 24.04) で実証済みの手順。冪等 (再実行で config/unit を更新)。
# サービスは enable のみ (24/7 起動は validate 後に手動 start)。
#
# 前提: /opt/icstv に icstv リポジトリの内容がある (agent/ と deploy/playout-node/)。
set -euo pipefail

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-v1.19.0}"
MEDIAMTX_URL="${MEDIAMTX_URL:-https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz}"
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"   # /opt/icstv
PN="$REPO_ROOT/deploy/playout-node"
export DEBIAN_FRONTEND=noninteractive

# このノードに同居させるチャンネル (slug, 空白区切り)。既定 ch1。
# 増やす場合は casparcg.config に対応する <channel> を足すこと。
# 宣言の無い channel を slug だけ足しても AMCP が解決できず動かない。
# 各 slug につき icstv-agent@<slug> / icstv-encoder@<slug> を enable し、env 雛形を生成する。
CHANNELS="${ICSTV_CHANNELS:-ch1}"

# exposure_policy (#27、docs/site-only-broadcast.md) の YTミラー(公開M/メンバーP)を導入するチャンネル
# (CHANNELS の部分集合, 空白区切り)。既定は空 (opt-in)。GPU 容量実測 (docs §5-1) が終わるまで
# 空のままにすること — 追加すると icstv-yt-mirror-encoder@<slug> / icstv-yt-members-encoder@<slug>
# の env 雛形生成 + enable (start はしない) が動く。例: ICSTV_YT_MIRROR_CHANNELS="ch1"。
YT_MIRROR_CHANNELS="${ICSTV_YT_MIRROR_CHANNELS:-}"

echo "==> [1/9] base packages + locale (CasparCG は有効な locale が必須)"
apt-get update
apt-get install -y curl ca-certificates git python3-venv python3-pip \
  locales fonts-dejavu-core iproute2 software-properties-common
locale-gen en_US.UTF-8 C.UTF-8 >/dev/null 2>&1 || true
update-locale LANG=C.UTF-8 || true

echo "==> [2/9] Intel VAAPI userspace + Mesa EGL (headless GL) + nginx (HLS ladder 静的配信)"
apt-get install -y \
  intel-media-va-driver-non-free libva2 libva-drm2 vainfo intel-gpu-tools \
  libegl1 libegl-mesa0 libgles2 libgbm1 libgl1-mesa-dri mesa-utils mesa-utils-extra \
  ffmpeg nginx-light

echo "==> [3/9] service users (render/video は host gid と 1:1。privileged CT)"
id casparcg >/dev/null 2>&1 || useradd -r -m -d /opt/casparcg -s /usr/sbin/nologin casparcg
id mediamtx >/dev/null 2>&1 || useradd -r -m -d /opt/mediamtx -s /usr/sbin/nologin mediamtx
usermod -aG render,video casparcg || true
echo "   render/video gids:"; getent group render video || true

echo "==> [4/9] VAAPI sanity (H264 EncSlice エントリポイント)"
if runuser -u casparcg -- env LIBVA_DRIVER_NAME=iHD vainfo --display drm --device /dev/dri/renderD128 2>/dev/null | grep -q 'VAEntrypointEncSlice'; then
  echo "   OK: H264 encode entrypoint あり"
else
  echo "   WARN: VAEntrypointEncSlice が見えない。dev0 gid / iHD ドライバを確認 (validate.sh Gate2)。" >&2
fi

echo "==> [5/9] CasparCG 2.5 (PPA。binary=/usr/bin/casparcg-server, scanner=/usr/bin/casparcg-scanner)"
add-apt-repository -y ppa:casparcg/ppa
apt-get update
apt-get install -y casparcg-server casparcg-scanner
mkdir -p /opt/casparcg/{media,log,data,template}
mkdir -p /opt/casparcg/media/{filler,slate,cm,asset,bumper}
# CEF CG テンプレート (提供/Lバー/テロップ/バンパー。casparcg.md §3) を template-path へ配置
if [[ -d "$PN/casparcg/template" ]]; then
  cp -r "$PN/casparcg/template/." /opt/casparcg/template/
fi
chown -R casparcg:casparcg /opt/casparcg

echo "==> [6/9] MediaMTX ${MEDIAMTX_VERSION}"
if [[ ! -x /opt/mediamtx/mediamtx ]]; then
  mkdir -p /opt/mediamtx
  tmp="$(mktemp -d)"; curl -fsSL "$MEDIAMTX_URL" -o "$tmp/m.tgz"
  tar -xzf "$tmp/m.tgz" -C "$tmp"; install -m755 "$tmp/mediamtx" /opt/mediamtx/mediamtx; rm -rf "$tmp"
fi
chown -R mediamtx:mediamtx /opt/mediamtx

echo "==> [7/9] icstv-agent venv (pip install -e agent)"
python3 -m venv /opt/icstv/agent/.venv
/opt/icstv/agent/.venv/bin/pip install --upgrade pip wheel >/dev/null
/opt/icstv/agent/.venv/bin/pip install -e /opt/icstv/agent

echo "==> [8/9] config / env / systemd 配置"
install -d -m755 /etc/icstv
install -m644 "$PN/casparcg/casparcg.config" /etc/icstv/casparcg.config
# casparcg-scanner は CWD(/opt/casparcg) の ./casparcg.config を読む (無いと ENOENT で起動失敗)。
# server は /etc/icstv/casparcg.config を明示指定するので、scanner 用に symlink を置く。
ln -sf /etc/icstv/casparcg.config /opt/casparcg/casparcg.config
install -m644 "$PN/mediamtx/mediamtx.yml"     /etc/icstv/mediamtx.yml

# HLS ABR ladder の静的配信 nginx (:8889, /hls2)。encoder が tmpfs /run/icstv-hls へ書き、ここが配信する。
install -m644 "$PN/nginx/icstv-hls.conf" /etc/nginx/conf.d/icstv-hls.conf
# Debian 既定サイト (:80) は不要なので外す (我々が使うのは :8889 のみ)。
rm -f /etc/nginx/sites-enabled/default

# 旧・単一チャンネル env (agent.env/encoder.env) があれば ch1 のチャンネル別 env へ移行 (秘密値を保全)。
[[ -f /etc/icstv/agent.env   && ! -f /etc/icstv/agent-ch1.env   ]] && install -m600 /etc/icstv/agent.env   /etc/icstv/agent-ch1.env
[[ -f /etc/icstv/encoder.env && ! -f /etc/icstv/encoder-ch1.env ]] && install -m600 /etc/icstv/encoder.env /etc/icstv/encoder-ch1.env

# チャンネル別 env の雛形を生成 (存在すれば温存)。実値 (TOKEN/CF キー/UDP_PORT/CASPAR_CHANNEL) は手動で埋める。
for ch in $CHANNELS; do
  [[ -f "/etc/icstv/agent-$ch.env" ]]   || install -m600 "$PN/env/agent.env.example"   "/etc/icstv/agent-$ch.env"
  [[ -f "/etc/icstv/encoder-$ch.env" ]] || install -m600 "$PN/env/encoder.env.example" "/etc/icstv/encoder-$ch.env"
done
# exposure_policy (#27) YTミラー env 雛形 (opt-in チャンネルのみ)。
for ch in $YT_MIRROR_CHANNELS; do
  [[ -f "/etc/icstv/yt-mirror-encoder-$ch.env" ]]  || install -m600 "$PN/env/yt-mirror-encoder.env.example"  "/etc/icstv/yt-mirror-encoder-$ch.env"
  [[ -f "/etc/icstv/yt-members-encoder-$ch.env" ]] || install -m600 "$PN/env/yt-members-encoder.env.example" "/etc/icstv/yt-members-encoder-$ch.env"
done

# テンプレートユニット (icstv-agent@.service / icstv-encoder@.service) を含む .service を配置。
# ただし先頭コメントに「# 未配備の理由:」を持つ unit は意図的な未配備 (例: YTミラー2種は GPU
# 容量待ち + casparcg.config が 1ch 宣言で入力 channel が無い) なので既定では配置しない。
# 無条件グロブだと再構築のたびに未配備方針が黙って崩れる。sync-node.sh も同じヘッダを読んで
# [未配備] と理由を表示する = 検査と配置の判定を同じ規約 (unit 側にヘッダを置く) に揃える。
for u in "$PN"/systemd/*.service; do
  if grep -q '^#[[:space:]]*未配備の理由:' "$u"; then
    echo "   skip $(basename "$u") (未配備の理由ヘッダあり。導入は opt-in 手順で)"
    continue
  fi
  install -m644 "$u" /etc/systemd/system/
done
# opt-in (ICSTV_YT_MIRROR_CHANNELS) が指定されたときだけ YTミラー unit を配置する。
if [[ -n "$YT_MIRROR_CHANNELS" ]]; then
  install -m644 "$PN"/systemd/icstv-yt-mirror-encoder@.service \
                "$PN"/systemd/icstv-yt-members-encoder@.service /etc/systemd/system/
fi
# 送出ヘルス監視 (icstv-watchdog.timer)。.timer も配置しないと watchdog が周期起動しない。
install -m644 "$PN"/systemd/*.timer /etc/systemd/system/
install -m755 "$PN"/scripts/watchdog.sh /usr/local/bin/icstv-watchdog.sh
# 出力健全性を Zabbix へ返す UserParameter 用スクリプト (黒落ち/セグメント停止の検知)。
install -m755 "$PN"/scripts/hls-health.sh /usr/local/bin/icstv-hls-health.sh
# zabbix-agent2 が入っているノードにだけ UserParameter を置く。エージェント自体の導入は
# ノード共通のプロビジョニング側の担当なので、ここでは設定の配置と反映だけ行う。
if [[ -d /etc/zabbix/zabbix_agent2.d ]]; then
  install -m644 "$PN"/zabbix/icstv-playout.conf /etc/zabbix/zabbix_agent2.d/icstv-playout.conf
  systemctl reload-or-restart zabbix-agent2 2>/dev/null || true
fi
# 旧・非テンプレートユニットが残っていれば停止・除去 (テンプレートと二重起動しないように)。
for old in icstv-agent icstv-encoder; do
  if systemctl list-unit-files "$old.service" 2>/dev/null | grep -q "$old.service"; then
    systemctl disable --now "$old.service" 2>/dev/null || true
    rm -f "/etc/systemd/system/$old.service"
  fi
done
systemctl daemon-reload
# set-property 由来の MemoryHigh ドリフトを撤去する。
# unit の MemoryMax より低い MemoryHigh が効くと throttle 域でプロセスが wedge し、
# オンエアが止まる。set-property は drop-in を消さず infinity に
# 書き換えるだけなので、判定はファイルの有無ではなく実効値で行う。
if [[ "$(systemctl show casparcg-server.service -p MemoryHigh --value 2>/dev/null)" != "infinity" ]]; then
  echo "   NOTE: MemoryHigh を無効化 (unit 側の MemoryMax に一本化)"
  systemctl set-property casparcg-server.service MemoryHigh= || true
fi
systemctl enable mediamtx casparcg-scanner casparcg-server
# watchdog は enable --now でよい (oneshot + timer。送出に副作用を与えない)。
systemctl enable --now icstv-watchdog.timer
# nginx (HLS ladder 静的配信) は即 enable+restart してよい (encoder と独立・無害)。
nginx -t && systemctl enable --now nginx && systemctl reload nginx || \
  echo "   WARN: nginx -t 失敗。/etc/nginx/conf.d/icstv-hls.conf を確認。" >&2
for ch in $CHANNELS; do
  systemctl enable "icstv-encoder@$ch" "icstv-agent@$ch" "icstv-hls-ladder@$ch"
done
# exposure_policy (#27) YTミラー encoder (opt-in チャンネルのみ。GPU 容量実測が終わるまで start しないこと)。
for ch in $YT_MIRROR_CHANNELS; do
  systemctl enable "icstv-yt-mirror-encoder@$ch" "icstv-yt-members-encoder@$ch"
done

echo "==> [9/9] placeholder スレート/案内フィラー (media/slate, media/filler)"
if [[ ! -f /opt/casparcg/media/slate/please_wait.png ]]; then
  ffmpeg -y -f lavfi -i color=c=black:s=1280x720 \
    -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='PLEASE WAIT':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2" \
    -frames:v 1 /opt/casparcg/media/slate/please_wait.png 2>/dev/null || true
  chown casparcg:casparcg /opt/casparcg/media/slate/please_wait.png 2>/dev/null || true
fi
# exposure_policy (#27) の YTミラー案内フィラー2種 (agent/icstv_agent/config.py の既定 clip 名と一致)。
# server の Channel.site_only_filler/members_filler manifest が優先され、これはノードローカル fallback。
if [[ ! -f /opt/casparcg/media/filler/site_only_default.png ]]; then
  ffmpeg -y -f lavfi -i color=c=black:s=1280x720 \
    -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='本編はサイト/メンバー限定配信中':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2" \
    -frames:v 1 /opt/casparcg/media/filler/site_only_default.png 2>/dev/null || true
  chown casparcg:casparcg /opt/casparcg/media/filler/site_only_default.png 2>/dev/null || true
fi
if [[ ! -f /opt/casparcg/media/filler/members_default.png ]]; then
  ffmpeg -y -f lavfi -i color=c=black:s=1280x720 \
    -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='次のメンバー限定番組まで':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2" \
    -frames:v 1 /opt/casparcg/media/filler/members_default.png 2>/dev/null || true
  chown casparcg:casparcg /opt/casparcg/media/filler/members_default.png 2>/dev/null || true
fi

cat <<EOF

==> install 完了 (channels: $CHANNELS)。次の手順 (チャンネルごとに):
  1. /etc/icstv/agent-<slug>.env   に ★per-channel: ICSTV_AGENT_TOKEN / ICSTV_CHANNEL_SLUG /
       ICSTV_CASPAR_CHANNEL / ICSTV_QUEUE_DB(=queue-<slug>.db) を設定
  2. /etc/icstv/encoder-<slug>.env に ★per-channel: UDP_PORT(=5004+(N-1)) / ICSTV_CHANNEL_SLUG /
       CF_INGEST_URL を設定
  3. 検証:   bash /opt/icstv/deploy/playout-node/scripts/validate.sh
  4. 起動:   systemctl start mediamtx casparcg-scanner casparcg-server
             for ch in $CHANNELS; do systemctl start icstv-encoder@\$ch icstv-agent@\$ch; done
EOF

if [[ -n "$YT_MIRROR_CHANNELS" ]]; then
  cat <<EOF

==> exposure_policy (#27) YTミラー opt-in: $YT_MIRROR_CHANNELS
  ** GPU 容量実測 (docs/site-only-broadcast.md §5-1) が終わるまで以下の start は行わないこと。**
  5. /etc/icstv/agent-<slug>.env に ICSTV_YT_MIRROR_CASPAR_CHANNEL / ICSTV_YT_MEMBERS_CASPAR_CHANNEL
       のコメントを外す (casparcg.config の channel 3/4 相当の番号)。
  6. /etc/icstv/yt-mirror-encoder-<slug>.env / yt-members-encoder-<slug>.env に UDP_PORT /
       YT_INGEST_URL / YT_MEMBERS_INGEST_URL を設定。
  7. GPU 容量実測後、起動: systemctl restart icstv-agent@<slug>
       systemctl start icstv-yt-mirror-encoder@<slug> icstv-yt-members-encoder@<slug>
  8. 本線 tee からの YouTube leg 除去 (カットオーバー) は
       deploy/playout-node/systemd/icstv-encoder@<slug>.service.d.example/ を参照 (検証ノード先行)。
EOF
fi
