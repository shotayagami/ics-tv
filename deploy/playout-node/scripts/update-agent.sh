#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 送出ノード (Proxmox ホスト上の LXC) の icstv-agent コードを反映する冪等デプロイ。
#
# なぜコピー方式か: agent は editable install (pip install -e /opt/icstv/agent) なので、
# パッケージファイルを直接置き換えて systemctl restart すれば反映される。
# ノードの /opt/icstv は git チェックアウト (sync-node.sh) なので、
# コミット済みの変更を反映するなら sync-node.sh + restart で足りる。本スクリプトの用途は
# 「まだコミットしていない手元の作業ツリーをノードで試す」場合に絞られる。
# server 側は GitOps コントローラ (k8s) で git 駆動だが、送出ノードは k8s 外のペットのためこの経路で反映する。
#
# 冪等性: sha256 差分のあるファイルだけ転送し、1 つでも変わったら restart。差分ゼロなら
# 転送も restart もせず exit 0。
#
# 再起動の安全性: run() は dispatch ループ前に caspar.connect() 済み (agent/icstv_agent/main.py)
# なので dispatch ループが「切断」を観測せず、現行イベントの再 take は走らない = 映像はシームレス
# (CasparCG は別プロセスで再生継続)。唯一の影響は restart 窓 (~1s) に重なった TAKE の ~1s 遅延
# (直後の dispatch で拾う)。番組頭飛びは起きないため、フィラー待ちガードは設けていない。
#
# スコープ: 同期するのは icstv_agent (パッケージ本体) + icstv_proto/icstv/v1 (proto生成物)。
# 依存追加 (要 pip install -e) はこのスクリプトの対象外で、別途フルデプロイが要る。
# **icstv_proto は必ず icstv_agent と一緒に同期すること**: 新規 PlayoutAction
# (PLAY_VT) 追加時に icstv_agent だけを反映したところ、その enum を参照する
# icstv_agent コードと enum 未定義のままの旧 icstv_proto が組み合わさり
# `AttributeError: module 'icstv.v1.playout_pb2' has no attribute 'PLAYOUT_ACTION_PLAY_VT'`
# でクラッシュループする事故が実際に起きている (gRPC 制御プレーン断・手動復旧)。
# 以後、proto (icstv_proto) もこのスクリプトの正式な同期対象に含める。
#
# 手動 (運用端末から) でも開発側の CI からでも同一スクリプトで動く。
# ただし開発側 CI の自動デプロイの発火条件は agent/icstv_agent/** の変更のみ (paths フィルタ)
# なので、icstv_proto だけを更新した場合は手動実行が要る。
#
# 環境変数:
#   PVE_SSH   Proxmox ホストへの ssh 先 (必須。未設定ならエラー終了。例 root@192.0.2.12)
#   CTID      送出 CT の id             (必須。未設定ならエラー終了。provision-lxc.sh で作成した CT)
#   SLUGS     restart するチャンネル    (既定 "ch1"、空白区切り。例 "ch1 ch2")
#   SSH_OPTS  追加 ssh オプション       (CI で "-i <key> -o StrictHostKeyChecking=accept-new" 等)
#   DRY_RUN   1 で差分表示のみ (転送/restart せず)
set -euo pipefail

PVE_SSH="${PVE_SSH:?PVE_SSH は必須です (Proxmox ホストへの ssh 先を指定する。例 root@192.0.2.12)}"
CTID="${CTID:?CTID は必須です (送出 CT の CTID を指定する)}"
SLUGS="${SLUGS:-ch1}"
DRY_RUN="${DRY_RUN:-0}"
read -r -a SSH_OPT_ARR <<< "${SSH_OPTS:-}"

# 個別 TCP 接続を積み上げると Proxmox ホストの sshd が接続数上限で後続を弾く (CI 環境で再現)ため、
# ControlMaster で全 SSH 呼び出しを 1 本の TCP コネクションに多重化する。
# ただしコネクションを多重化しても「セッション数」(sshd MaxSessions、既定10) は
# 個別に消費されるため、1 ファイル 1 セッションの呼び出しを積み重ねると
# ファイル数が MaxSessions を超えた時点でセッションが拒否され、フォールバック
# (|| true) がそれを「差分あり」に誤判定した挙句コネクション自体が切断される
# (実際に発生している・agent/icstv_agent が14ファイルに増えて顕在化した)。
# 対策: 差分チェック/転送/反映を少数セッションに集約し、ファイル数に依存させない。
#
# 注意: ssh は末尾の複数引数をスペース結合してリモートシェルに渡すため、
# `ctexec sh -c "... '$X' ... 2>/dev/null"` のように 1 引数の中に埋め込んだ
# シェル構文 (glob/リダイレクト) は結合時に単語境界が失われて壊れる (実際に
# `sh -c sha256sum ...` の "..." 部分が捨てられ sha256sum が引数無しで
# stdin を読みに行く事故で発覚)。よって ctexec には常にプレーンな argv
# (glob/リダイレクト無し) だけを渡す。glob 相当は find の -name で代替し、
# 複雑なロジックは argv ではなく heredoc 経由の stdin スクリプト (bash -s) で渡す。
_SSH_CTL="${RUNNER_TEMP:-/tmp}/ssh_ctl_playout_$$"
SSH_OPT_ARR+=(-o "ControlMaster=auto" -o "ControlPath=${_SSH_CTL}" -o "ControlPersist=30")
_cleanup_ssh() { ssh -q -o "ControlPath=${_SSH_CTL}" -O exit "$PVE_SSH" 2>/dev/null || true; }
trap _cleanup_ssh EXIT

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../agent" && pwd)"

# 同期対象パッケージ (両方揃わないと import 時にクラッシュしうるため、常にペアで扱う)。
PKG_NAMES=(icstv_agent icstv_proto)
declare -A PKG_SRC=(
  [icstv_agent]="$AGENT_ROOT/icstv_agent"
  [icstv_proto]="$AGENT_ROOT/icstv_proto/icstv/v1"
)
declare -A PKG_DEST=(
  [icstv_agent]="/opt/icstv/agent/icstv_agent"
  [icstv_proto]="/opt/icstv/agent/icstv_proto/icstv/v1"
)

# CT 内でコマンド実行 (ssh <proxmox-host> → pct exec CTID)。引数は単純語のみ (リダイレクトは渡さない)。
ctexec() { ssh "${SSH_OPT_ARR[@]}" "$PVE_SSH" pct exec "$CTID" -- "$@"; }

sha_local() { sha256sum "$1" | cut -d' ' -f1; }

echo "==> agent 反映: $AGENT_ROOT -> $PVE_SSH CT$CTID (slugs: $SLUGS)"

# 1) パッケージ毎に sha256 で差分ファイルを洗い出す (ノードに無いファイルも差分扱い=新規)。
#    各パッケージのハッシュを1セッションでまとめて取得 (ファイル数に依らずパッケージ数=2回)。
#    find の -name はシェル glob ではなく find 自身のパターン照合なので、
#    ctexec にプレーンな argv のまま渡せる (sh -c 経由の埋め込み構文が不要)。
declare -A CHANGED
total_changed=0
for pkg in "${PKG_NAMES[@]}"; do
  src="${PKG_SRC[$pkg]}"
  dest="${PKG_DEST[$pkg]}"
  declare -A node_sha=()
  while read -r hash fname; do
    [ -n "$hash" ] && node_sha["$(basename "$fname")"]="$hash"
  done < <(ctexec find "$dest" -maxdepth 1 -name '*.py' -exec sha256sum '{}' + 2>/dev/null)

  changed=()
  for path in "$src"/*.py; do
    f="$(basename "$path")"
    if [ "$(sha_local "$path")" != "${node_sha[$f]:-}" ]; then
      echo "   diff: $pkg/$f"
      changed+=("$f")
    fi
  done
  CHANGED[$pkg]="${changed[*]}"
  total_changed=$((total_changed + ${#changed[@]}))
done

if [ "$total_changed" -eq 0 ]; then
  echo "==> 差分なし (ノードは既に最新) — restart せず終了"
  exit 0
fi
echo "==> 差分 ${total_changed} 件"

if [ "$DRY_RUN" = "1" ]; then
  echo "==> DRY_RUN: 転送/restart はしない"
  exit 0
fi

# 2) 変更ファイルをパッケージ毎にまとめて tar 転送でテンポラリへ送る (パッケージ毎に
#    mkdir 1 + tar展開 1 = 最大 4セッション、ファイル数に依らず定数)。ssh/tcp が既に
#    転送の完全性を保証するため、旧実装のようなファイル毎の sha 再照合セッションは不要。
#    テンポラリ止まりの間は稼働中ファイルに一切触れないため、途中で失敗しても稼働中
#    agent は無傷。mkdir と tar 展開を分けているのも、ctexec に "&&" 入りの1引数を
#    渡すと同じ理由 (ssh の引数結合) で壊れるため。
TMPROOT="/tmp/icstv-agent-update.$$"
ctexec mkdir -p "$TMPROOT"

combined_changed=()
for pkg in "${PKG_NAMES[@]}"; do
  read -r -a changed <<< "${CHANGED[$pkg]}"
  [ "${#changed[@]}" -eq 0 ] && continue
  ctexec mkdir -p "$TMPROOT/$pkg"
  tar -cf - -C "${PKG_SRC[$pkg]}" "${changed[@]}" | ctexec tar -xf - -C "$TMPROOT/$pkg"
  for f in "${changed[@]}"; do
    combined_changed+=("$pkg:$f")
  done
done

# 3) mv + restart + 起動確認を1セッションのリモートスクリプトにまとめる。
#    全ファイルを mv してから restart、restart 後に active を確認する。
#    dest マッピングはローカルの PKG_DEST と対応させて埋め込む (パッケージは2つで固定、
#    動的に渡すほどの数ではないため)。
if ctexec bash -s -- "$TMPROOT" "$SLUGS" "${combined_changed[@]}" <<'REMOTE_EOF'
set -euo pipefail
tmp="$1"; shift
slugs="$1"; shift
for entry in "$@"; do
  pkg="${entry%%:*}"
  f="${entry#*:}"
  case "$pkg" in
    icstv_agent) dest="/opt/icstv/agent/icstv_agent" ;;
    icstv_proto) dest="/opt/icstv/agent/icstv_proto/icstv/v1" ;;
    *) echo "!! 未知のパッケージ: $pkg" >&2; exit 1 ;;
  esac
  mv "$tmp/$pkg/$f" "$dest/$f"
done
rm -rf "$tmp"
rc=0
for slug in $slugs; do
  echo "==> restart icstv-agent@$slug"
  systemctl restart "icstv-agent@$slug"
done
for slug in $slugs; do
  state="$(systemctl is-active "icstv-agent@$slug" || true)"
  echo "   icstv-agent@$slug: $state"
  [ "$state" = "active" ] || rc=1
done
exit "$rc"
REMOTE_EOF
then
  echo "==> 反映完了 (${total_changed} 件)"
else
  echo "!! 一部 slug が active でない"
  exit 1
fi
