#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
# 送出ノードの /opt/icstv を git チェックアウトとして同期し、配備済み成果物との差分を報告する。
#
# なぜ必要か:
#   /opt/icstv は長らく provision 時のスナップショットコピーで git 管理外だった。そのため
#   deploy/playout-node/ 配下の更新をノードへ取り込む経路が「install.sh の全体再実行」しか無く、
#   install.sh は apt でのパッケージ再取得や venv 再構築まで行うプロビジョニングスクリプトなので
#   稼働中の送出ノードには流せない。結果としてノードのソースが数か月古いまま放置され、
#   いざ反映するときに無関係な変更まで巻き込む温床になっていた (2026-08-18 に顕在化)。
#
# 設計方針:
#   - 稼働中のサービスには一切触らない。fetch / checkout と差分レポートのみ。
#   - 実際の反映 (ファイル配置と restart) は意図的に手動に残す。断が出る操作を暗黙に走らせない。
#   - 冪等。何度実行しても同じ結果になる。
#   - 自分自身も checkout 対象なので、更新されたら新しい方へ exec し直す (後述の自己更新対策)。
#
# 使い方 (ノード内で root 実行):
#   bash sync-node.sh              # 既定 ref (main) へ同期して差分を表示
#   bash sync-node.sh dev          # ref を指定
#
# 認証: リポジトリが public なら匿名 clone で足りる。private 化した場合は
#   読み取り専用 deploy key を作り ICSTV_REPO_URL に ssh:// 形式を渡すこと。
#
# ICSTV_REPO_URL は必ず自分の環境に合わせて指定すること (下記既定値は未設定検知用のダミー)。
set -uo pipefail

REPO_URL="${ICSTV_REPO_URL:-https://<git-host>/<your-org>/icstv.git}"
REF="${1:-${ICSTV_REF:-main}}"
DEST="${ICSTV_NODE_SRC:-/opt/icstv}"

PASS=0; DRIFT=0

# 自己更新対策 (1/2): checkout 前の自分自身のハッシュを控える。
# このスクリプトは checkout 対象そのものなので、実行中に自分が置き換わることがある。bash は
# 起動時に開いた inode を読み続けるため、放置すると**スクリプトを更新した回だけ 1 世代前の
# 挙動で走り切る** (2026-08-22 に、更新したはずの表示が旧文言のまま出て実際に踏んだ)。
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
self_hash(){ [[ -f "$SELF" ]] && sha256sum "$SELF" 2>/dev/null | cut -d' ' -f1; }
SELF_HASH_BEFORE="$(self_hash)"
ok(){   echo "  [同一] $1"; PASS=$((PASS+1)); }
diffy(){ echo "  [差分] $1"; DRIFT=$((DRIFT+1)); }

echo "==> [1/3] /opt/icstv を git チェックアウト化 / 更新 (ref=$REF)"
if [[ ! -d "$DEST/.git" ]]; then
  echo "    git 管理外。スナップショットを git チェックアウトへ移行する。"
  echo "    追跡外のファイル (.venv / egg-info / media / log / *.tgz バックアップ) は保持される。"
  git -C "$DEST" init -q
  git -C "$DEST" remote add origin "$REPO_URL"
else
  echo "    既に git チェックアウト。fetch して更新する。"
fi
git -C "$DEST" fetch -q --depth=1 origin "$REF" || { echo "    ERROR: fetch 失敗 ($REPO_URL $REF)" >&2; exit 1; }

# index だけを FETCH_HEAD に合わせる (作業ツリーは触らない)。こうすると初回移行時 =
# HEAD がまだ無いスナップショット状態でも、作業ツリーとの実差分を正しく取れる。
# HEAD 同士の比較にすると初回は比較対象が無く、agent の巻き戻りを見逃す。
git -C "$DEST" reset -q FETCH_HEAD
AGENT_CHANGED=0
git -C "$DEST" diff --name-only -- agent/icstv_agent agent/icstv_proto | grep -q . && AGENT_CHANGED=1
CHANGED_N="$(git -C "$DEST" diff --name-only | wc -l)"
echo "    作業ツリーと ref の差分: ${CHANGED_N} ファイル (追跡外は保持)"

git -C "$DEST" checkout -q -f FETCH_HEAD
git -C "$DEST" branch -q -f "$REF" FETCH_HEAD 2>/dev/null || true
echo "    HEAD = $(git -C "$DEST" log --oneline -1)"

# 自己更新対策 (2/2): checkout で自分が変わっていたら新しい方で実行し直す。
# 常に ref の内容どおりに動くことを保証する。チェックアウト外から実行したコピー (例: /tmp の
# 検証用) はハッシュが変わらないので再 exec されない。ICSTV_SYNC_REEXECED はループ止め
# (再 exec 後は同じ内容なのでハッシュ比較でも止まるが、二重の保険)。
if [[ -z "${ICSTV_SYNC_REEXECED:-}" && -n "$SELF_HASH_BEFORE" && "$(self_hash)" != "$SELF_HASH_BEFORE" ]]; then
  echo "    このスクリプト自身が更新された -> 新しい方で実行し直す"
  export ICSTV_SYNC_REEXECED=1
  exec bash "$SELF" "$@"
fi

echo
echo "==> [2/3] 配備済み成果物との差分 (リポジトリ → 実際にインストールされている場所)"
cmp_one(){ # $1=リポジトリ相対パス $2=配備先
  local src="$DEST/deploy/playout-node/$1" dst="$2"
  [[ -f "$src" ]] || return 0
  if [[ ! -e "$dst" ]]; then diffy "$(basename "$dst") (未配置)"; return 0; fi
  if cmp -s "$src" "$dst"; then ok "$(basename "$dst")"; else diffy "$(basename "$dst")"; fi
}
cmp_one casparcg/casparcg.config /etc/icstv/casparcg.config
cmp_one mediamtx/mediamtx.yml    /etc/icstv/mediamtx.yml
cmp_one nginx/icstv-hls.conf     /etc/nginx/conf.d/icstv-hls.conf
cmp_one scripts/watchdog.sh      /usr/local/bin/icstv-watchdog.sh
cmp_one scripts/hls-health.sh    /usr/local/bin/icstv-hls-health.sh
cmp_one zabbix/icstv-playout.conf /etc/zabbix/zabbix_agent2.d/icstv-playout.conf

# CasparCG CG テンプレート (casparcg/template/**)。install.sh は `cp -r` + `chown -R casparcg:casparcg`
# で一括配置するため cmp_one 1本では見えず、これまで sync-node.sh は個別ファイルの差分を検知して
# いなかった (2026-09-02 是正)。git ls-files ベースで template-path 配下と 1 ファイルずつ突き合わせる。
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  sub="${rel#deploy/playout-node/casparcg/template/}"
  dst="/opt/casparcg/template/$sub"
  if [[ ! -e "$dst" ]]; then diffy "template/$sub (未配備)"; continue; fi
  cmp -s "$DEST/$rel" "$dst" && ok "template/$sub" || diffy "template/$sub"
done < <(git -C "$DEST" ls-files -- 'deploy/playout-node/casparcg/template/*')

# 列挙は git 管理下のファイルに限る。ファイルグロブで拾ってはいけない:
# スナップショット時代の残骸が追跡外ファイルとして残っており (checkout は追跡外を消さない)、
# グロブだと「リポジトリから既に削除された古い unit」を現役のソースとして比較・案内してしまう
# (実例: icstv-agent.service / icstv-encoder.service の非テンプレート版)。
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  b="$(basename "$rel")"
  # 実機に無い unit は「意図的に配備していない」ことがある。理由は unit 自身の先頭コメントに
  # `# 未配備の理由: ...` として書いておき、ここで読み出して表示する。理由を本スクリプトに
  # 持たせるとドリフトするので、必ず unit 側に置くこと。
  # (以前は一律「opt-in の可能性あり」と出しており、実際は GPU 容量待ちのものを運用上の選択と
  #  読み違える余地があった)
  if [[ ! -e "/etc/systemd/system/$b" ]]; then
    why="$(sed -n 's/^#[[:space:]]*未配備の理由:[[:space:]]*//p' "$DEST/$rel" | head -1)"
    if [[ -n "$why" ]]; then
      echo "  [未配備] $b — $why"
    else
      echo "  [未配備] $b (理由の記載なし。unit の先頭コメントを確認すること)"
    fi
    continue
  fi
  cmp -s "$DEST/$rel" "/etc/systemd/system/$b" && ok "$b" || diffy "$b"
done < <(git -C "$DEST" ls-files -- 'deploy/playout-node/systemd/*.service' 'deploy/playout-node/systemd/*.timer')

# CG テンプレ (casparcg/template/)。CasparCG の template-path (/opt/casparcg/template/) へは
# install.sh が初回プロビジョニング時にのみ配置するため、以後の追加・変更はここで検出しない限り
# 気づけない。実例: 2026-07-04 追加の map/tsunami-corner.html が差分レポート対象外だったため
# 2 か月未配備のまま残り、2026-07-28 の実津波注意報で CG ADD が File not found になった。
# テンプレに unit のような opt-in は無い (リポジトリにあるものは全てノードに要る) ので、
# 未配置も [差分] と同格で DRIFT に数える。
TPL_ROOT="${ICSTV_CASPAR_TEMPLATE_DIR:-/opt/casparcg/template}"
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  t="${rel#deploy/playout-node/casparcg/template/}"
  if [[ ! -e "$TPL_ROOT/$t" ]]; then diffy "template/$t (未配置)"; continue; fi
  cmp -s "$DEST/$rel" "$TPL_ROOT/$t" && ok "template/$t" || diffy "template/$t"
done < <(git -C "$DEST" ls-files -- 'deploy/playout-node/casparcg/template/**')

# 追跡外の残骸を警告する。誤って古いファイルを配備する事故の元なので可視化しておく。
# deploy/ 限定だと agent/ 配下の *.bak や agent-code.bak*.tgz (実機に多数実在) を見逃すため
# 全ツリーを見る。.gitignore 対象 (.venv / egg-info 等) は clean -nd に出ないので保持される。
STALE="$(git -C "$DEST" clean -nd 2>/dev/null | sed 's/^Would remove /    /')"
if [[ -n "$STALE" ]]; then
  echo
  echo "  ! /opt/icstv に git 管理外の残骸がある (スナップショット時代の遺物や手動バックアップ):"
  echo "$STALE"
  echo "    掃除する場合 (追跡外のみ削除。.gitignore 対象の .venv 等は保持される):"
  echo "      git -C $DEST clean -fd"
fi

# /etc/systemd/system 側の git 外残骸 (旧非テンプレート unit や *.bak)。リポジトリ管理下の
# unit/timer 名に無い icstv-*/casparcg-*/mediamtx* と *.bak を列挙する。ここに残った旧 unit を
# 現役と取り違えて編集・enable する事故の元 (drop-in の .d ディレクトリは意図的な手動設定が
# あり得るため対象外)。/etc/icstv の *.bak も同様に手動掃除の対象。
KNOWN_UNITS="$(git -C "$DEST" ls-files -- 'deploy/playout-node/systemd/*' | while IFS= read -r k; do basename "$k"; done)"
ETC_STALE=""
# glob は重複マッチし得る (例: icstv-*.bak は icstv-* と *.bak* の両方に当たる) ので basename で重複除去する。
ETC_SEEN=$'\n'
for f in /etc/systemd/system/icstv-* /etc/systemd/system/casparcg-* /etc/systemd/system/mediamtx* /etc/systemd/system/*.bak*; do
  [[ -f "$f" ]] || continue
  b="$(basename "$f")"
  [[ "$ETC_SEEN" == *$'\n'"$b"$'\n'* ]] && continue
  ETC_SEEN+="$b"$'\n'
  grep -qxF "$b" <<<"$KNOWN_UNITS" && continue
  ETC_STALE+="    $f"$'\n'
done
if [[ -n "$ETC_STALE" ]]; then
  echo
  echo "  ! /etc/systemd/system に git 管理外の unit/バックアップがある (旧世代の残骸の可能性):"
  printf '%s' "$ETC_STALE"
  echo "    現役の unit は git 管理下の deploy/playout-node/systemd/ のみ。不要なら rm +"
  echo "    systemctl daemon-reload で掃除する (判断はオペレータ)。"
fi

echo
echo "==> [3/3] 結果: 同一=$PASS 差分=$DRIFT"
if [[ "$AGENT_CHANGED" -eq 1 ]]; then
  echo "  ! agent のコードが更新された (editable install なので配置は不要)。"
  echo "    反映するには: systemctl restart 'icstv-agent@*'"
fi
if [[ "$DRIFT" -gt 0 ]]; then
  cat <<'HINT'
  差分の反映は手動で行うこと (install.sh は稼働中のノードに流さない)。
    unit:      install -m644 <src> /etc/systemd/system/<name> && systemctl daemon-reload
    watchdog:  install -m755 <src> /usr/local/bin/icstv-watchdog.sh
    hls-health: install -m755 <src> /usr/local/bin/icstv-hls-health.sh (restart 不要)
    zabbix:    install -m644 <src> /etc/zabbix/zabbix_agent2.d/<name> の後 zabbix-agent2 reload
    template:  install -D -m644 -o casparcg -g casparcg <src> /opt/casparcg/template/<rel>
               (CasparCG の restart 不要。CEF は CG ADD 時にファイルを読む)
    config:    install -m644 <src> <dst> の後、該当サービスの restart が要る = 断が出る
    template:  install -m644 -o casparcg -g casparcg <src> /opt/casparcg/template/<rel> (CasparCG restart 不要。
               新規サブディレクトリ配下に足すときは先に mkdir -p + chown casparcg:casparcg が要る)
  casparcg.config を差し替えると casparcg-server の再起動が必要で、オンエアが切れる。
HINT
fi
exit 0
