#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 送出ノードの段階検証 (CT 内 root 実行)。各ゲートを順に確認。失敗ゲートで止める。
# docs/overview.md §7 / プランの Verification に対応。
set -uo pipefail
PASS=0; FAIL=0
ok(){ echo "  [OK] $1"; PASS=$((PASS+1)); }
ng(){ echo "  [NG] $1" >&2; FAIL=$((FAIL+1)); }

echo "== Gate 1: /dev/dri と gid =="
if [[ -e /dev/dri/renderD128 ]]; then ok "renderD128 present"; ls -l /dev/dri; else ng "renderD128 が無い (LXC dev0 共有を確認)"; fi
id casparcg | grep -qE 'render|video' && ok "casparcg in render/video" || ng "casparcg が render/video に未所属"

echo "== Gate 2: VAAPI H264 encode entrypoint =="
if LIBVA_DRIVER_NAME=iHD vainfo --display drm --device /dev/dri/renderD128 2>/dev/null | grep -q 'VAEntrypointEncSlice'; then
  ok "H264 VAEntrypointEncSlice あり"
else ng "encode entrypoint 無し (iHD ドライバ / gid を確認)"; fi

echo "== Gate 3: 単体 VAAPI エンコード (CasparCG 抜きで CF egress を実証) =="
echo "  手動: ffmpeg -f lavfi -i testsrc=size=1280x720:rate=60 -f lavfi -i sine \\"
echo "         -vaapi_device /dev/dri/renderD128 -vf format=nv12,hwupload -c:v h264_vaapi \\"
echo "         -b:v 4500k -c:a aac -f flv \"\$CF_INGEST_URL\"   → CF dashboard にバーが出れば OK"

echo "== Gate 4: CasparCG headless EGL 起動 =="
if systemctl is-active --quiet casparcg-server; then
  # 起動ログは全ジャーナルを走査 (稼働時間が延びると -n 窓から落ちるため)。
  if journalctl -u casparcg-server --no-pager 2>/dev/null | grep -qiE 'Initialized channels|OpenGL 4\.[0-9].*Compatibility'; then
    ok "EGL 初期化 (OpenGL Compat) + channels 初期化ログあり"
  # フォールバック: AMCP INFO がチャンネル行を返せばチャンネルは初期化済み (ログが期限切れでも可)。
  elif printf 'INFO\r\n' | timeout 3 nc 127.0.0.1 5250 2>/dev/null | grep -qE '[0-9]+ +[0-9]+p[0-9]+'; then
    ok "AMCP INFO でチャンネル稼働を確認 (起動ログは期限切れ)"
  else ng "channel 初期化未確認。EGL エラー時は unit の Environment=EGL_PLATFORM=surfaceless を確認 (保険: llvmpipe / 2.3.3+Xvfb)"; fi
else ng "casparcg-server 非稼働 (systemctl start 後に再確認)"; fi

echo "== Gate 5: AMCP 応答 (5250) =="
if printf 'VERSION\r\n' | timeout 3 nc 127.0.0.1 5250 2>/dev/null | grep -q .; then ok "AMCP VERSION 応答"; else ng "AMCP 無応答"; fi

echo "== Gate 6: CG テンプレ スモーク (提供 ADD/PLAY/UPDATE/STOP) =="
SMOKE=/opt/icstv/deploy/playout-node/scripts/amcp-smoke.py
if [[ -x /opt/icstv/agent/.venv/bin/python && -f "$SMOKE" ]]; then
  if /opt/icstv/agent/.venv/bin/python "$SMOKE" >/dev/null 2>&1; then
    ok "CG テンプレ (credit/sponsor) が ADD/PLAY/STOP に応答"
  else ng "CG スモーク失敗 (template-path に template/ 配置・casparcg 起動を確認)"; fi
else ng "agent venv / amcp-smoke.py 未配置 (install.sh 後に再確認)"; fi

echo "== Gate 7: MediaMTX API =="
if curl -fsS http://127.0.0.1:9997/v3/paths/list >/dev/null 2>&1; then ok "MediaMTX API 応答"; else ng "MediaMTX API 無応答"; fi

echo "== Gate 8: agent → control plane (チャンネルごと) =="
# 対象は /etc/icstv/agent-<slug>.env から導出。1 つも無ければ未配備として NG。
shopt -s nullglob
agent_envs=(/etc/icstv/agent-*.env)
if [[ ${#agent_envs[@]} -eq 0 ]]; then
  ng "agent env (/etc/icstv/agent-<slug>.env) が無い (install.sh 後に再確認)"
else
  for envf in "${agent_envs[@]}"; do
    slug="$(basename "$envf" .env)"; slug="${slug#agent-}"
    if systemctl is-active --quiet "icstv-agent@$slug"; then
      journalctl -u "icstv-agent@$slug" -n 30 --no-pager 2>/dev/null | grep -qiE 'heartbeat|subscribe|connected' \
        && ok "agent@$slug 接続ログあり" || ng "agent@$slug 接続ログ未確認 (ICSTV_SERVER_GRPC/AGENT_TOKEN を確認)"
    else ng "icstv-agent@$slug 非稼働"; fi
  done
fi

echo "== Gate 9: HLS ABR ladder (nginx :8889 / encoder tmpfs) =="
# nginx 静的配信が生きているか
if curl -fsS http://127.0.0.1:8889/healthz 2>/dev/null | grep -q ok; then ok "nginx :8889 応答"; else ng "nginx :8889 無応答 (systemctl status nginx / nginx -t)"; fi
# チャンネルごとに master.m3u8 と variant を確認 (encoder 稼働時のみ)。
shopt -s nullglob
enc_envs=(/etc/icstv/encoder-*.env)
for envf in "${enc_envs[@]}"; do
  slug="$(basename "$envf" .env)"; slug="${slug#encoder-}"
  systemctl is-active --quiet "icstv-hls-ladder@$slug" || { echo "  [..] hls-ladder@$slug 非稼働 (start 後に再確認)"; continue; }
  master="http://127.0.0.1:8889/hls2/$slug/master.m3u8"
  inf="$(curl -fsS "$master" 2>/dev/null | grep -c '^#EXT-X-STREAM-INF' || true)"
  if [[ "${inf:-0}" -ge 4 ]]; then ok "master@$slug に variant $inf 本 (720p/480p/144p/音声のみ)"; else ng "master@$slug の variant が $inf 本 (期待 4。encoder ログ/ffmpeg を確認)"; fi
  # フラット variant playlist (720p.m3u8) が読めるか + セグメント先頭の SPS/PPS (global_header 落とし穴)
  if command -v ffprobe >/dev/null 2>&1; then
    ffprobe -v error -i "http://127.0.0.1:8889/hls2/$slug/720p.m3u8" -show_entries stream=codec_type,width,height -of csv 2>/dev/null | grep -q video \
      && ok "ffprobe で 720p@$slug の映像を解析可" || ng "ffprobe が 720p@$slug を解析不可"
  fi
done
# iGPU 使用率の目安 (手動確認用ヒント)
echo "  ヒント: intel_gpu_top -s 1000 で Video エンジン<80% を確認 (超過時は encoder-<slug>.env を削減)"

echo "== Gate 10: 送出ヘルス監視 (icstv-watchdog.timer) =="
# 2026-08-18 のオンエア断は「watchdog.sh は存在するが配備されていない」ことが実害になった。
# スクリプトの存在ではなく timer が動いているかを見る。
if systemctl is-active --quiet icstv-watchdog.timer; then ok "icstv-watchdog.timer 稼働"; else ng "icstv-watchdog.timer 非稼働 (systemctl enable --now icstv-watchdog.timer)"; fi
[[ -x /usr/local/bin/icstv-watchdog.sh ]] && ok "icstv-watchdog.sh 配置済" || ng "/usr/local/bin/icstv-watchdog.sh が無い (install.sh を再実行)"
# ss パーサ (YouTube 枝 bytes_sent 監視) のユニット検証。配備物 (配備前なら見送り) に対して実行。
if [[ -x /usr/local/bin/icstv-watchdog.sh ]]; then
  # 旧版 (--self-test 非対応) は引数を無視して実監視 1 tick を root 実行してしまい
  # (restart 副作用の恐れ + 正常時 exit 0 の偽 OK)、必ず対応判定を先に挟む。
  if ! grep -q -- '--self-test' /usr/local/bin/icstv-watchdog.sh; then
    ng "配備済み watchdog が --self-test 未対応 (旧版。install -m755 で更新要)"
  elif bash /usr/local/bin/icstv-watchdog.sh --self-test >/dev/null 2>&1; then
    ok "watchdog self-test (ss パーサ) 成功"
  else
    ng "watchdog self-test 失敗 (bash /usr/local/bin/icstv-watchdog.sh --self-test で詳細)"
  fi
fi
# MemoryHigh が効いていると throttle 域で wedge する。unit の MemoryMax に一本化されているか。
# set-property は drop-in を削除せず MemoryHigh=infinity に書き換えるだけなので、
# ファイルの有無ではなく実効値で判定すること。
mh="$(systemctl show casparcg-server.service -p MemoryHigh --value 2>/dev/null)"
if [[ -z "$mh" || "$mh" == "infinity" ]]; then
  ok "casparcg-server の MemoryHigh は無効 (throttle 由来の wedge を回避)"
else
  ng "casparcg-server に MemoryHigh=$mh が効いている (systemctl set-property casparcg-server.service MemoryHigh=)"
fi

echo; echo "== 結果: PASS=$PASS FAIL=$FAIL =="
[[ $FAIL -eq 0 ]]
