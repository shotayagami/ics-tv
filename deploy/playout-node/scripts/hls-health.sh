#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 送出出力 (HLS ladder) の健全性を数値で返す。Zabbix の UserParameter から呼ぶ。
#
#   icstv-hls-health.sh <slug> age     最新セグメントの経過秒 (整数)
#   icstv-hls-health.sh <slug> black   1 つ前の 144p セグメントの黒フレーム比率 (%, 小数1桁)
#   icstv-hls-health.sh <slug> slate   スレート層 (N-90) が出ていれば 1、出ていなければ 0
#
# 判定不能 (ディレクトリ/セグメント無し、ffmpeg 失敗) はいずれも -1 を返し、終了コードは
# 常に 0 にする。Zabbix 側で「取得失敗」と「異常値」を区別できるようにするため。
#
# ■ なぜ 2 本要るか
#   black だけでは casparcg の wedge を取り逃す。入力が枯れるとセグメント生成自体が止まり、
#   最後の正常フレームを見続けて 0% を返し続ける (2026-08-18 のオンエア断がこの形)。
#   age だけでは層の消失を取り逃す。セグメントは 2 秒ごとに正常に出続け、中身だけが黒に
#   なる (2026-08-21 の 35 分黒落ち)。どちらか一方では今までの実障害を覆えない。
#
# ■ なぜ 144p か
#   黒の判定に解像度は要らず、送出ノードは CPU が逼迫している (4 コアで load 6 前後)。
#   実測 user 0.06s / sys 0.03s / elapsed 0.10s。720p はデータ量が 20 倍になる。
#
# ■ なぜ「1 つ前」のセグメントか
#   最新セグメントは muxer が書き込み中の可能性があり、途中まで書けたファイルを解析すると
#   デコード失敗や偽の黒を返す。age は鮮度が知りたいので最新を、black は中身を読むので
#   1 つ前を採る。
#
# ■ なぜ slate が要るか
#   休止帯のスレート (slate/please_wait) は黒背景に小さな白文字で、144p では文字が全画素の
#   2% に満たない。blackdetect の picture_black_ratio_th は既定 0.98 なので、正常なスレートが
#   「黒」と判定される (2026-08-22 00:03 の放送終了と同時に誤発報し、4 時間 PROBLEM のままだった)。
#   閾値を締めるのは 144p での文字の画素占有率に依存して脆いため、「スレートが出ているか」を
#   別のシグナルとして返し、トリガ側で黒の判定から除外する。判定規則は watchdog.sh と同じく
#   agent 実装 (icstv_agent.caspar.parse_foreground) に委ねる。
#
# ■ 発報のしかた
#   フィラー境界の dip to black は正常な絵なので、単発の黒で発報してはいけない。
#   ヒステリシスは Zabbix のトリガ側 (連続 N 回) で持たせること。
#   黒のトリガは slate=0 を条件に加えること (スレート表示中の黒は正常)。
set -uo pipefail

SLUG="${1:-ch1}"
METRIC="${2:-}"
HLS_DIR="${ICSTV_HLS_DIR:-/run/icstv-hls}/$SLUG"
CASPAR_HOST=127.0.0.1; CASPAR_PORT=5250
SLATE_LAYER=90
# 判定を agent 実装に委ねるため venv の python を使う。venv が無い/壊れていれば -1 (判定不能)。
AGENT_PY="${ICSTV_AGENT_PY:-/opt/icstv/agent/.venv/bin/python}"

# 数値以外を返すと Zabbix 側が「サポート対象外」にしてアイテムごと停止するため、
# どの失敗経路でも必ず数値を出して正常終了する。
unknown(){ echo "-1"; exit 0; }

# AMCP へ INFO を投げ、XML の終端 (</channel>) を見た時点で返す。
# nc は応答後も接続を保持するため `timeout 3 nc` だと常に 3 秒かかり、Zabbix agent の
# 既定 Timeout (3s) を超えてアイテムがタイムアウトする。終端で抜ける実装にして即返す。
# casparcg が wedge していれば read がタイムアウトし、部分出力 → 解析不能 → -1 になる。
amcp_info(){ # $1=channel $2=layer
  local line out=""
  exec 3<>"/dev/tcp/$CASPAR_HOST/$CASPAR_PORT" 2>/dev/null || return 1
  printf 'INFO %s-%s\r\n' "$1" "$2" >&3
  while IFS= read -r -t 2 line <&3; do
    out+="$line"$'\n'
    [[ "$line" == *"</channel>"* ]] && break
  done
  exec 3<&- 3>&-
  printf '%s' "$out"
}

[[ -d "$HLS_DIR" ]] || unknown

case "$METRIC" in
  age)
    newest="$(ls -t "$HLS_DIR"/*.ts 2>/dev/null | head -1)"
    [[ -n "$newest" ]] || unknown
    mtime="$(stat -c %Y "$newest" 2>/dev/null)" || unknown
    [[ -n "$mtime" ]] || unknown
    echo $(( $(date +%s) - mtime ))
    ;;
  black)
    seg="$(ls -t "$HLS_DIR"/144p_*.ts 2>/dev/null | sed -n 2p)"
    [[ -n "$seg" ]] || unknown
    out="$(timeout 15 ffmpeg -nostdin -v info -i "$seg" \
             -vf blackdetect=d=0.05:pix_th=0.10 -an -f null - 2>&1)" || unknown
    # "Duration: 00:00:02.03," / "[blackdetect @ ..] .. black_duration:1.96667"
    dur="$(printf '%s\n' "$out" | awk -F'Duration: |,' '/Duration:/{print $2; exit}' \
             | awk -F: '{printf "%.3f", ($1*3600)+($2*60)+$3}')"
    blk="$(printf '%s\n' "$out" | awk -F'black_duration:' \
             '/black_duration:/{s+=$2} END{printf "%.3f", s+0}')"
    awk -v b="$blk" -v d="$dur" 'BEGIN{ if (d+0 <= 0) print -1; else printf "%.1f", (b/d)*100 }'
    echo
    ;;
  slate)
    # チャンネル番号は agent の env から採る (既定 1)。
    cc="$(sed -n 's/^ICSTV_CASPAR_CHANNEL=\([0-9][0-9]*\).*/\1/p' \
           "/etc/icstv/agent-$SLUG.env" 2>/dev/null | head -1)"
    [[ -n "$cc" ]] || cc=1
    # 接続失敗時に bash 自身が出すエラーは exec への 2>/dev/null では消えないので呼び出し側で捨てる。
    xml="$(amcp_info "$cc" "$SLATE_LAYER" 2>/dev/null)"
    [[ -n "$xml" ]] || unknown
    printf '%s' "$xml" | "$AGENT_PY" -c '
import sys
from icstv_agent.caspar import parse_foreground
fg = parse_foreground(sys.stdin.read(), int(sys.argv[1]))
print(-1 if fg is None else (0 if fg["producer"] == "empty" else 1))
' "$SLATE_LAYER" 2>/dev/null || unknown
    ;;
  *)
    unknown
    ;;
esac
