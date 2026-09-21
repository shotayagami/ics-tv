#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 送出ヘルス監視 (icstv-watchdog.timer が 30s 周期で起動)。docs/overview.md §6。
# 各層を確認し、異常時は段階的に再起動へエスカレーションする。
#
# CasparCG が memory.high スロットルで wedge しオンエアが止まる形を受けて追加:
#   - 天井に当たる前の計画的リサイクル (CASPAR_MEM_RECYCLE_BYTES)
#   - wedge 時は SIGTERM が効かないため SIGKILL へエスカレーション
#   - AMCP の単発タイムアウトで 24h 配信を落とさないための連続失敗回数判定
#   - HLS セグメント鮮度 = 「実際にオンエアされているか」の最も直接的なシグナル
#
# 本線が黒のまま続く (黒落ち) 形を受けて修正:
#   - 本線停止の判定が壊れていた (後述の layer_state を参照)。導入以来一度も真にならず、
#     本線が消えても検知できていなかった
#   - 緊急スレートへの退避を廃止し、casparcg 再起動で agent に再 take させる方式へ変更
#   - agent 自身の生存確認を追加 (encoder と MediaMTX は見ていたが agent は見ていなかった)
#
# YouTube 無配信 (tee の YouTube 枝だけ凍結) を受けて追加:
#   - encoder の YouTube 宛 RTMP ソケットの bytes_sent 進行監視 (check_yt_bytes)。
#     ffmpeg は active、ローカル (MediaMTX) 枝は正常、encoder unit も HLS 鮮度も健全なまま
#     YouTube 枝だけが送出を止めており、既存のどの検査にも掛からなかった。
#     ss で見ると YouTube 宛ソケットは ESTAB のまま bytes_sent が停滞 = カーネルの
#     TCP 統計で「実際にバイトが流れているか」を直接見るのが唯一確実な検出だった。
# 検知結果は logger 経由で journal に出る。journal をログ基盤へ転送していればそのまま
# アラート化できる。CRIT 相当 (視聴者影響が出ている確定異常) は crit() = journal priority crit
# で出すので、転送側で priority をラベル化しておけば重大度で絞り込める。
set -uo pipefail

CASPAR_HOST=127.0.0.1; CASPAR_PORT=5250
STATE_DIR=/run/icstv-watchdog
CASPAR_CGROUP=/sys/fs/cgroup/system.slice/casparcg-server.service/memory.current
HLS_ROOT=/run/icstv-hls

# AMCP 無応答をこの回数連続で観測してから再起動する (30s 周期 x 3 = 約 90s 継続で発火)。
AMCP_FAIL_THRESHOLD=3
# casparcg のメモリがこれを超えたら MemoryMax(3G) に当たる前にリサイクルする。
# 実測: 復帰直後 547MiB → 約 6.5h で 2.5G の throttle 域に到達。2.2G は天井まで余裕を残した位置。
CASPAR_MEM_RECYCLE_BYTES=$((2200 * 1024 * 1024))
# HLS セグメントがこの秒数以上更新されていなければオンエア断とみなす (segment 2s に対し十分な余裕)。
HLS_STALE_SEC=30
# 本線が黒のまま何回連続で観測したら介入するか (30s 周期 x 3 = 約 90s)。
# agent の出力 watchdog が 3s x 3 tick = 約 9s で再 take するはずなので、それを待ってから動く。
MAIN_BLACK_TICKS=3
# 黒への介入の最短間隔。復旧しない黒 (再 take 対象のイベントが無い等) で casparcg を
# 90 秒ごとに再起動し続ける事態を避ける。
MAIN_BLACK_COOLDOWN_SEC=600
# 本線の状態判定は agent 実装に委ねる。venv が無い/壊れている場合は unknown となり介入しない。
AGENT_PY="${ICSTV_AGENT_PY:-/opt/icstv/agent/.venv/bin/python}"

# YouTube 枝 bytes_sent がこの秒数進まなければ凍結とみなす (上記の凍結の検出用)。
# 通常の送出では 30s tick ごとに必ず数 MiB 進む。5 分は YouTube 側の一時的な
# 受信スロットル (snd_wnd 縮小で数十秒詰まることはある) を誤検知しない余裕。
YT_BYTES_STALL_SEC=${YT_BYTES_STALL_SEC:-300}
# encoder 自動 restart の最短間隔 (安全弁)。restart は YouTube/ローカル両枝の断になるため、
# 復旧しない停滞で 5 分ごとに再起動し続ける事態を避ける。
YT_RESTART_COOLDOWN_SEC=${YT_RESTART_COOLDOWN_SEC:-1800}
# 連続でこの回数 restart しても復旧しなければ諦めて CRIT ログのみ (根本原因が
# encoder の外 [ingest URL 失効・経路断など] にあり、restart では直らない形)。
YT_RESTART_MAX_ATTEMPTS=${YT_RESTART_MAX_ATTEMPTS:-3}

# ss -H -tinp 'dport = :1935' の出力から、指定 pid が持つ「非 loopback 宛」ソケットの
# bytes_sent 合計を出す。マッチするソケットが 1 つも無ければ何も出力しない。
# dport 1935 の非 loopback = YouTube ingest (CF_INGEST_URL)。127.0.0.1:1935 はローカル
# MediaMTX 枝、および icstv-hls-ladder が MediaMTX から読む側のソケットなので除外する。
#
# 出力形式は実測 (iproute2-6.1.0) に合わせる。ソケットごとに
# 「行頭が非空白のソケット行」+「行頭が空白の TCP 情報行」の 2 行組:
#   ESTAB 0 2399 192.0.2.20:59946 142.251.118.134:1935 users:(("ffmpeg",pid=478219,fd=6))
#        cubic wscale:8,10 ... bytes_sent:8288138821 bytes_retrans:191662 bytes_acked:...
# bytes_retrans / bytes_acked を bytes_sent と取り違えないよう、キー名は完全一致で拾う。
parse_yt_bytes(){ # $1=pid / stdin=ss 出力
  awk -v pid="$1" '
    /^[^[:space:]]/ {
      matched = 0
      peer = $5
      sub(/:[0-9]+$/, "", peer)   # ポートを落とす ([::1]:1935 → [::1])
      gsub(/[][]/, "", peer)      # IPv6 の角括弧を落とす
      if (peer !~ /^127\./ && peer != "::1" && index($0, "pid=" pid ",") > 0) matched = 1
      next
    }
    matched {
      if (match($0, /(^|[[:space:]])bytes_sent:[0-9]+/)) {
        s = substr($0, RSTART, RLENGTH)
        sub(/.*bytes_sent:/, "", s)
        total += s
        found = 1
      }
      matched = 0
    }
    END { if (found) print total + 0 }
  '
}

# self-test: パーサを実測フィクスチャで検証する (root 不要・システム無変更)。
# 実行: bash watchdog.sh --self-test
if [[ "${1:-}" == "--self-test" ]]; then
  # 実ノードで採取した ss -H -tinp 'dport = :1935' の出力。
  # pid=478219 = icstv-encoder@ch1 の ffmpeg (fd5=ローカル MediaMTX 枝, fd6=YouTube 枝)、
  # pid=478329 = icstv-hls-ladder の ffmpeg (MediaMTX から読む側で bytes_sent はほぼ 0)。
  fixture='ESTAB 0      0          127.0.0.1:33308       127.0.0.1:1935 users:(("ffmpeg",pid=478219,fd=5))
	 cubic wscale:10,10 rto:201 rtt:0.045/0.036 mss:65483 cwnd:10 bytes_sent:8225054422 bytes_acked:8225054423 bytes_received:3467 segs_out:1304015
ESTAB 0      0          127.0.0.1:33310       127.0.0.1:1935 users:(("ffmpeg",pid=478329,fd=3))
	 cubic wscale:10,10 rto:211 rtt:10.064/14.618 mss:39936 cwnd:10 bytes_sent:54542 bytes_acked:54543 bytes_received:8220231094
ESTAB 0      2399   192.0.2.20:59946 142.251.118.134:1935 users:(("ffmpeg",pid=478219,fd=6))
	 cubic wscale:8,10 rto:205 rtt:4.35/0.18 mss:1360 cwnd:120 ssthresh:77 bytes_sent:8288138821 bytes_retrans:191662 bytes_acked:8287945792 notsent:1031'
  # 再接続直後などで同一 pid の YouTube 宛ソケットが複数 ESTAB のケース (合算を確認)
  fixture_multi='ESTAB 0 0 192.0.2.20:59946 142.251.118.134:1935 users:(("ffmpeg",pid=100,fd=6))
	 cubic bytes_sent:1000 bytes_acked:900
ESTAB 0 0 192.0.2.20:59948 [2404:6800:4004::c]:1935 users:(("ffmpeg",pid=100,fd=7))
	 cubic bytes_sent:23 bytes_acked:23'
  fails=0
  expect(){ # $1=説明 $2=期待値 $3=実際
    if [[ "$3" == "$2" ]]; then echo "ok: $1"; else echo "NG: $1 (期待 '$2' 実際 '$3')"; fails=$((fails+1)); fi
  }
  expect "encoder pid → YouTube 枝のみ (loopback 2 本を除外)" "8288138821" \
    "$(printf '%s\n' "$fixture" | parse_yt_bytes 478219)"
  expect "ladder pid → 非 loopback 無し = 出力なし" "" \
    "$(printf '%s\n' "$fixture" | parse_yt_bytes 478329)"
  expect "無関係 pid → 出力なし" "" \
    "$(printf '%s\n' "$fixture" | parse_yt_bytes 999999)"
  expect "同一 pid の複数ソケット (IPv6 込み) は合算" "1023" \
    "$(printf '%s\n' "$fixture_multi" | parse_yt_bytes 100)"
  expect "空入力 → 出力なし" "" "$(printf '' | parse_yt_bytes 478219)"
  if [[ "$fails" -eq 0 ]]; then echo "self-test: 全て成功"; exit 0; else echo "self-test: ${fails} 件失敗"; exit 1; fi
fi

mkdir -p "$STATE_DIR"

log(){ logger -t icstv-watchdog "$*"; }
# 視聴者影響が出ている確定異常。journal priority=crit で出すので、ログ基盤へ転送していれば
# 重大度で絞り込める。
crit(){ logger -p user.crit -t icstv-watchdog "$*"; }
amcp(){ printf '%s\r\n' "$1" | timeout 3 nc "$CASPAR_HOST" "$CASPAR_PORT" 2>/dev/null; }

# 指定 layer の状態を playing / empty / unknown で返す。
# empty は「その層に何も載っていない」であって、本線 (10) に限れば黒であることを意味する。
#
# 判定規則は agent と同一の実装 (icstv_agent.caspar.parse_foreground) を使う。shell 側で
# XML を grep すると必ず食い違うため:
#   - INFO {ch}-{layer} はチャンネル全体の XML を「改行込みで」返すので、行単位の grep で
#     <foreground> と <producer>empty</producer> を同時に見ることはできない
#   - 占有されていない層は <layer_NN> ブロックごと出ないため、そもそも「foreground が empty」
#     という形にならない。casparcg 再起動直後がまさにこの形
# 旧実装の `grep -qiE 'foreground.*empty|stopped'` は上記のため導入以来一度も真にならず、
# 本線停止検知は機能していなかった。
layer_state(){ # $1=channel $2=layer
  local xml
  xml="$(amcp "INFO ${1}-${2}")"
  [[ -n "$xml" ]] || { echo unknown; return; }
  printf '%s' "$xml" | "$AGENT_PY" -c '
import sys
from icstv_agent.caspar import parse_foreground
fg = parse_foreground(sys.stdin.read(), int(sys.argv[1]))
print("unknown" if fg is None else ("empty" if fg["producer"] == "empty" else "playing"))
' "$2" 2>/dev/null || echo unknown
}

# wedge した casparcg は SIGTERM に応答せず、systemctl restart が TimeoutStopSec まで固まる。
# SIGKILL で落とせば Restart=on-failure が拾って起こす (実地で確認)。
caspar_restart(){
  log "casparcg-server 再起動: $1"
  if ! timeout 25 systemctl restart casparcg-server.service; then
    log "restart が 25s で完了せず -> SIGKILL へエスカレーション"
    systemctl kill -s SIGKILL casparcg-server.service
    sleep 3
    systemctl is-active --quiet casparcg-server.service || systemctl start casparcg-server.service
  fi
  rm -f "$STATE_DIR/amcp_fail"
}

# encoder の YouTube 枝が実際にバイトを流しているかの監視 (凍結の再発防止)。
# tee の fifo 自動復帰は「エラーになれば」再接続するが、ソケットが ESTAB のまま送出だけが
# 止まる形 (エラーにならない凍結) では、ffmpeg 内のどの復帰機構も発火しない。
# unit の外から TCP 統計 (bytes_sent) を見て、進まなければ encoder ごと restart する。
check_yt_bytes(){ # $1=slug
  local slug="$1" pid cur prev prev_ts now stalled attempts last
  pid="$(systemctl show -p MainPID --value "icstv-encoder@$slug" 2>/dev/null)"
  # MainPID が取れない/0 = encoder が居ない。生存はこの関数の役目ではない (上で restart 済み)。
  # ffmpeg の再起動で pid が変わった場合は bytes_sent の値も変わる = 進行扱いになるので安全。
  if [[ -z "$pid" || "$pid" == 0 ]]; then rm -f "$STATE_DIR/yt_bytes_$slug"; return; fi
  cur="$(ss -H -tinp 'dport = :1935' 2>/dev/null | parse_yt_bytes "$pid")"
  now="$(date +%s)"
  # YouTube 宛ソケット不在は正常扱い。放送休止帯で YouTube 出力が意図的に無い形や、
  # YTミラーへのカットオーバー drop-in (encoder@.service 冒頭コメント参照) で encoder が
  # MediaMTX 枝のみの形がこれに当たる。「ソケットが存在するのに進まない」だけを異常とする。
  # (凍結中はソケットが ESTAB で残り続けるのが実測。枝が本当に落ちれば
  #  ソケットは消え、fifo の attempt_recovery が再接続を試みる = ffmpeg 側の守備範囲。)
  if [[ -z "$cur" ]]; then rm -f "$STATE_DIR/yt_bytes_$slug"; return; fi
  { read -r prev prev_ts < "$STATE_DIR/yt_bytes_$slug"; } 2>/dev/null || { prev=""; prev_ts="$now"; }
  if [[ "$cur" != "$prev" ]]; then
    echo "$cur $now" > "$STATE_DIR/yt_bytes_$slug"
    # restart 後、停滞閾値ぶん進行が持続してはじめて「復旧成功」とみなし失敗カウンタを畳む。
    # 即時に畳むと「restart 直後の握手数 KiB だけ流れてまた凍る」形が毎回 1 回目扱いになり、
    # 上限 (YT_RESTART_MAX_ATTEMPTS) が意味を失うため。
    if [[ -s "$STATE_DIR/yt_attempts_$slug" ]]; then
      last="$(cat "$STATE_DIR/yt_acted_$slug" 2>/dev/null || echo 0)"
      [[ $(( now - last )) -ge "$YT_BYTES_STALL_SEC" ]] && rm -f "$STATE_DIR/yt_attempts_$slug"
    fi
    return
  fi
  stalled=$(( now - prev_ts ))
  [[ "$stalled" -ge "$YT_BYTES_STALL_SEC" ]] || return
  attempts="$(cat "$STATE_DIR/yt_attempts_$slug" 2>/dev/null || echo 0)"
  if [[ "$attempts" -ge "$YT_RESTART_MAX_ATTEMPTS" ]]; then
    crit "YouTube 枝 bytes_sent 停滞 ch=$slug ${stalled}s: 自動 restart ${attempts} 回でも復旧せず -> 自動介入を停止中 (手動対応が必要)"
    return
  fi
  crit "YouTube 枝 bytes_sent 停滞 ch=$slug ${stalled}s (閾値 ${YT_BYTES_STALL_SEC}s, bytes_sent=${cur}, pid=${pid})"
  # 過去に介入していれば最短間隔 (cooldown) を守る。初回は即介入。
  last="$(cat "$STATE_DIR/yt_acted_$slug" 2>/dev/null || echo '')"
  if [[ -n "$last" && $(( now - last )) -lt "$YT_RESTART_COOLDOWN_SEC" ]]; then
    log "YouTube 枝停滞 ch=$slug: restart cooldown 中 (前回介入から $(( now - last ))s < ${YT_RESTART_COOLDOWN_SEC}s)"
    return
  fi
  echo "$now" > "$STATE_DIR/yt_acted_$slug"
  echo $(( attempts + 1 )) > "$STATE_DIR/yt_attempts_$slug"
  rm -f "$STATE_DIR/yt_bytes_$slug"
  crit "YouTube 枝停滞 ch=$slug -> icstv-encoder@$slug restart ($(( attempts + 1 ))/${YT_RESTART_MAX_ATTEMPTS} 回目)"
  systemctl restart "icstv-encoder@$slug"
}

# 0) 計画的リサイクル。AMCP がまだ生きているうちに回すのが肝で、
#    throttle 域に入ってからでは stop も start も詰まる。
if [[ -r "$CASPAR_CGROUP" ]]; then
  mem="$(cat "$CASPAR_CGROUP" 2>/dev/null || echo 0)"
  if [[ "$mem" -gt "$CASPAR_MEM_RECYCLE_BYTES" ]]; then
    caspar_restart "メモリ $((mem/1048576))MiB > 閾値 $((CASPAR_MEM_RECYCLE_BYTES/1048576))MiB (計画的リサイクル)"
    exit 0
  fi
fi

# 1) AMCP 生存 (全チャンネル共有の単一 CasparCG サーバ)。
#    wedge 時は TCP connect だけ成功して応答が返らないため、ポート死活監視では捕まらない。
#    実際にコマンドを投げて応答の有無を見る。
if amcp 'VERSION' | grep -q .; then
  rm -f "$STATE_DIR/amcp_fail"
else
  fails=$(( $(cat "$STATE_DIR/amcp_fail" 2>/dev/null || echo 0) + 1 ))
  echo "$fails" > "$STATE_DIR/amcp_fail"
  log "AMCP 無応答 ($fails/$AMCP_FAIL_THRESHOLD)"
  [[ "$fails" -ge "$AMCP_FAIL_THRESHOLD" ]] && caspar_restart "AMCP 無応答 x${fails}"
  exit 0
fi

# 2,3) チャンネルごと: 本線(N-10)停止 → 緊急スレート(N-90)、encoder@<slug> 生存。
# 対象チャンネルは /etc/icstv/agent-<slug>.env から導出 (N=ICSTV_CASPAR_CHANNEL)。
shopt -s nullglob
for envf in /etc/icstv/agent-*.env; do
  slug="$(basename "$envf" .env)"; slug="${slug#agent-}"
  # 意図的に停止しているチャンネルは対象外。env ファイルは disable 後も残置されるため、
  # このガードが無いと disabled な encoder@<slug> を 30 秒ごとに起こし続ける
  # (disable した後も env ファイルだけ残っているチャンネルが該当)。
  systemctl is-enabled --quiet "icstv-encoder@$slug" 2>/dev/null || continue
  cc="$(sed -n 's/^ICSTV_CASPAR_CHANNEL=\([0-9][0-9]*\).*/\1/p' "$envf" | head -1)"
  [[ -n "$cc" ]] || cc=1
  # agent 自身の生存。落ちていると TAKE も再 take も走らず、次の予定まで画面が固まる。
  # encoder と MediaMTX は見ていたが agent は見ていなかった。
  if ! systemctl is-active --quiet "icstv-agent@$slug"; then
    log "icstv-agent@$slug 停止 -> restart"
    systemctl restart "icstv-agent@$slug"
  fi
  # 本線(N-10)が黒か。ただし層 90 にスレートが出ていれば視聴者にはスレートが見えている
  # (休止帯の off_air スレート / 手動の緊急スレート) ので黒ではなく、介入しない。
  if [[ "$(layer_state "$cc" 10)" == "empty" && "$(layer_state "$cc" 90)" != "playing" ]]; then
    blacks=$(( $(cat "$STATE_DIR/black_$slug" 2>/dev/null || echo 0) + 1 ))
    echo "$blacks" > "$STATE_DIR/black_$slug"
    log "本線が黒 ch=$slug (caspar $cc) ($blacks/$MAIN_BLACK_TICKS)"
    if [[ "$blacks" -ge "$MAIN_BLACK_TICKS" ]]; then
      last="$(cat "$STATE_DIR/black_acted_$slug" 2>/dev/null || echo 0)"
      since=$(( $(date +%s) - last ))
      if [[ "$since" -ge "$MAIN_BLACK_COOLDOWN_SEC" ]]; then
        date +%s > "$STATE_DIR/black_acted_$slug"
        rm -f "$STATE_DIR/black_$slug"
        # casparcg を落とすと agent が「切断→再接続」を観測し、現行イベントを本線へ貼り直す。
        # agent の restart では復旧しない: 起動時に接続済みだと現行を貼り直さない設計のため
        # (それが正しい。通常の agent 更新でオンエアを触らせないための性質)。
        #
        # 旧実装のように緊急スレート (PLAY N-90) へ退避してはいけない。層 90 は層 10 を覆うので、
        # 上げると次の予定 TAKE が来ても画面はスレートのままになる。解除は編成の CLEAR_SLATE
        # イベントだけで、放送中に上げてしまうと次の休止帯まで数時間塞がる。黒より悪い。
        caspar_restart "本線が黒のまま ${blacks} 回 ch=$slug (agent の再 take が効いていない)"
        exit 0
      fi
      log "本線が黒 ch=$slug だが cooldown 中 (前回の介入から ${since}s)"
    fi
  else
    rm -f "$STATE_DIR/black_$slug"
  fi
  # サイドカーエンコーダ (CF egress) 生存
  if ! systemctl is-active --quiet "icstv-encoder@$slug"; then
    log "encoder@$slug 停止 -> restart"
    systemctl restart "icstv-encoder@$slug"
    continue  # restart 直後の bytes_sent 監視は無意味 (次 tick から見る)
  fi
  # encoder が active でも YouTube 枝だけ凍結していないか (上記の凍結の形)
  check_yt_bytes "$slug"
done

# 4) MediaMTX: API 死活に加えて publish 中の path があるかを見る。
#    API が正常応答したまま itemCount:0 (publisher 不在) になる形があり、
#    死活のみでは断を検知できない。
paths_json="$(curl -fsS --max-time 5 http://127.0.0.1:9997/v3/paths/list 2>/dev/null)"
if [[ -z "$paths_json" ]]; then
  log "MediaMTX API 無応答 -> restart"
  systemctl restart mediamtx
elif ! printf '%s' "$paths_json" | grep -q '"ready":true'; then
  log "MediaMTX に ready な path が無い (publisher 不在の疑い)"
fi

# 5) HLS セグメント鮮度。encoder が active でも中身が止まっていることがあるため
#    systemctl is-active では足りない。ここは検知専用で自動復旧はしない
#    (ladder 自身が再試行するので、二重に再起動を掛けると振動するため)。
now="$(date +%s)"
for d in "$HLS_ROOT"/*/; do
  [[ -d "$d" ]] || continue
  slug="$(basename "$d")"
  newest="$(find "$d" -maxdepth 1 -name '*.ts' -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)"
  if [[ -z "$newest" ]]; then
    log "HLS セグメント無し ch=$slug (オンエア断の疑い)"
    continue
  fi
  age=$(( now - newest ))
  [[ "$age" -gt "$HLS_STALE_SEC" ]] && log "HLS セグメント停滞 ch=$slug ${age}s (閾値 ${HLS_STALE_SEC}s)"
done

exit 0
