# 開発ワークフロー

## セットアップ (初回)

```bash
# server: Django + 周辺
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install --with-deps chromium   # E2E を回す場合だけ

# agent: 独立パッケージ
cd ../agent
python3 -m venv .venv
.venv/bin/pip install -e .

# frontend: React モノレポ (Node 24)。studio SPA / 公開島のビルドに必要
cd ../frontend
npm ci

# pre-commit hook (Commit 時の品質ゲートを有効化)
cd ..
pip install --user pre-commit
pre-commit install
```

- **buf CLI** も導入しておく (`buf lint` が pre-commit hook に入っており、無いと proto を
  触る commit がブロックされる)。proto を変更したら `buf generate` を実行する —
  `server/icstv_proto` と `agent/icstv_proto` の**両方**が更新される (再生成漏れの罠は
  `docs/site-only-broadcast.md` を参照。ここでは二重管理しない)。
- 生成したコードにも SPDX ライセンスヘッダが要る。`buf generate` (protobuf の生成コード) や
  `makemigrations` (migration) の後は、リポジトリルートで `python3 tools/spdx_headers.py --fix` を実行する。
  引数を省くと `git ls-files` に載っているファイルだけが対象になるため、まだ `git add` していない
  新しい migration はパスを指定する (例: `python3 tools/spdx_headers.py --fix server/<app>/migrations/<新しいファイル>.py`)。
  `npm run gen:api` は `schema.ts` へのヘッダ付与まで自動で行う (そのため python3 が要る)。
  ヘッダが欠けたファイルがあると CI が失敗する。
- ローカルで Web 画面 (studio SPA / `/app/`) を触るには、venv とは別に **frontend の
  ビルド成果物を `server/frontend_dist` へ配置**する必要がある (手順は `docs/usage.md` §4。
  未配置だと `/studio/` 空白・`/app/` 503)。

## 環境変数 (.env) の共用

機密 (R2 / Google OAuth / Cloudflare 等) は `~/.env` に書いておく。
**キーの一覧の正本は `server/.env.example`** (settings.py が読む env キーを網羅)。
本番の実値は導入者の配備基盤側 (非機密は ConfigMap 相当、機密は Secret 相当) が持つ。

- **docker compose (dev)**: `server/.env` へ **cp する** (docker-compose.yml ヘッダの方式):
  ```
  cp ~/.env server/.env        # または cp server/.env.example server/.env
  ```
  symlink (`ln -sf ~/.env server/.env`) は**不可** — コンテナ内でリンク先が存在しない
  dangling symlink になり、django-environ は read_env の OSError を握り潰して**黙って無視する**
  (エラーも出ずに全キー未設定で起動する)。
- **本番/staging**: 機密は導入者の配備基盤側の Secret として配る。生成手順・出力先・対象キーの
  一覧はその基盤側の正本に置くことになるが、**その一式はこのツリーには含まれない**。このツリーでの
  キー一覧の正本は上記の `server/.env.example` で、Secret の作成方法は導入先の基盤に合わせる。
  **Secret を生成するスクリプトを用意する場合、生成されるキー集合が本番 Secret の全キーを
  カバーしない食い違いが起きやすい**ので、キーの過不足は `server/.env.example` と突き合わせること。

## ブランチ運用

main への直接 push は禁止。変更は必ず PR 経由にする。

1. `git switch main && git pull` で最新を取得
2. `git switch -c feature/xxx` (機能追加) または `fix/xxx` (修正) で作業ブランチを切る
3. 実装 → commit → push。PR を出す (base は **main**)
4. CI (test・agent・grpc・audit・frontend・e2e) が green になったらレビューを経て merge

**緊急時 (致命的障害)** の hotfix も同じ PR フローに従う。検証を挟む余裕が無い場合は、
その旨を PR に明記した上でレビューを急ぐ。

> 実際にこのコードを動かすときの image のビルド・push・タグ運用・環境間の昇格は、
> **導入者の配備基盤側の話であり、このツリーには含まれない** (このツリーの CI はイメージの
> push も署名も行わない。`docs/usage.md` §7 を参照)。

## レジストリの保持ポリシー (開発側の記述)

> 注記: この節は**導入者が自分のコンテナレジストリを運用する場合**の設計上の注意で、
> このツリーの CI には含まれない (このツリーはイメージを push しない)。

コンテナレジストリの保持ポリシー (retention) は「直近 push N 件を保護」「直近 N 日以内に
push されたものを保護」といったルールの OR 結合で組むのが一般的で、そこに
「`v*` タグ限定で直近 N 件を保護」のルールを足すことが多い。

**タグ限定のルールは、その形式のタグを 1 件でも持つ repository にしか効かない。** sha7 の
コミットタグしか無い repository は、push 頻度次第で他のルールの枠から古い順に落ち、
実質無防備になる。運用者側でこの形の事故 (本番 pin していたタグがレジストリから削除され、
稼働中の Pod がノードのキャッシュだけで動き続けた/pin していないリポジトリの定期実行が
まとめて停止した) が起きたため、記録として残す。

**pin (本番が固定して参照するタグ) は必ず `v*` の形式にする** (sha7 やブランチの moving tag
を pin にしない)。pin bump 前には次の両方を確認してから切り替える:

- `crane digest <registry>/<project>/server:vX.Y.Z` — digest が存在すること
- 署名を必須にしている場合は、対応する署名 (`sha256-<digest>.sig` タグ等) が存在すること

確認せずに pin を切り替えると、存在しないイメージを指すか、署名検証のポリシーに拒否される。

## Commit ワークフロー

```
1. コード変更
2. AI コードレビュー (Claude Code 上で)
       $ claude
       > /code-review
   → 指摘項目を反映
3. git add ... && git commit -m "..."
   pre-commit hook が以下を自動実行:
     - ruff (lint + format)
     - mypy (型)
     - buf lint (proto)
     - detect-secrets (機密漏れ)
     - django check + makemigrations --check
     - trailing-whitespace 等の衛生
   いずれかが失敗 → commit は中断、修正して再 commit
4. (推奨) push 前にフルテスト
       $ sudo docker compose --profile test build test
       $ sudo docker compose run --rm web python manage.py test scheduling
       $ sudo docker compose --profile test run --rm test pytest tests/api/
       $ sudo docker compose --profile test run --rm test pytest -m e2e tests/e2e/
5. git push
```

## 機械チェックの内訳 (`.pre-commit-config.yaml`)

| hook | 目的 | 失敗時の対応 |
|------|------|--------------|
| `ruff --fix` | lint (pycodestyle / pyflakes / isort / pyupgrade / bugbear) | hook が自動修正、再 add してから commit |
| `ruff-format` | formatter (black 互換) | 同上 |
| `mypy` | 型チェック (Django plugin 込み) | 型を直す。Phase 1 は段階導入で非アノテーションは許容 |
| `buf lint` | proto 規約 (buf CLI が未導入だと proto を触る commit 自体が失敗する) | proto を書き直す |
| `detect-secrets` | 機密漏れ | 誤検知なら値の行末に `# pragma: allowlist secret` を付ける (`.env` 系は行末コメントが値に混入するため、前の行に独立して `# pragma: allowlist nextline secret` を置く)。baseline ファイルは使わない |
| `django check` | system check (DB 不要) | 設定/モデル定義を直す |
| `django makemigrations --check` | model 変更が migration 化されているか | `cd server && .venv/bin/python manage.py makemigrations` の後、リポジトリルートで `python3 tools/spdx_headers.py --fix server/<app>/migrations/<新しいファイル>.py` を実行してから migration を commit |

`mypy` / `django check` 系は `server/.venv` を前提とする。CONTRIBUTING.md のセットアップを完了してから実行する。

## AI コードレビュー (`/code-review`)

Claude Code 上で `/code-review` を起動すると、ステージしている差分を「再利用・品質・効率」観点でレビューし、見つかった issue を修正してくれる。
変更が大きい時 (新規ファイル / 100 行超の改変 / 公開 API 追加) には commit 前に必ず通すこと。
小さい修正 (typo / 単純 rename) は省略してよい。

セキュリティ観点でのレビューは `/security-review` を別途呼ぶ (`auth` / `crypto` / SQL を触ったとき)。

## ローカルテストの cheat sheet

```bash
# Django DB 制約テスト (TransactionTestCase, PostgreSQL 必須)
sudo docker compose run --rm web python manage.py test scheduling

# HTTP smoke (Django test Client)
sudo docker compose --profile test run --rm test pytest tests/api/test_http_views.py

# gRPC smoke (要 grpc サービス起動)
sudo docker compose up -d postgres grpc
sudo docker compose --profile test run --rm test pytest -m grpc tests/api/test_grpc_smoke.py

# Playwright E2E (live_server + chromium)
# ⚠ 先に docs/usage.md §4 の frontend build + server/frontend_dist への配置を実行しておくこと
#   (.github/workflows/ci.yml の e2e job と同一手順。未配置だと SPA 島のテストが frontend not built で落ちる)
sudo docker compose --profile test run --rm test pytest -m e2e tests/e2e/
```
