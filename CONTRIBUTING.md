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

| ブランチ | 役割 | デプロイ先 |
|---|---|---|
| `main` | 本番環境で稼働中のソース | 本番環境 (main 追従) |
| `dev` | 検証環境のソース (常設) | 検証環境 (dev 追従) |
| `feature/*` / `fix/*` | 機能追加・修正の作業ブランチ (dev 起点) | (デプロイ無し) |

```
feature/* ──▶ dev ──(検証 OK)──▶ main
(dev 起点)     staging で確認      本番リリース
```

**通常フロー (機能追加・修正)**

1. `git switch dev && git pull` で最新の dev を取得
2. `git switch -c feature/xxx` で作業ブランチを切る (**dev 起点**)
3. 実装 → commit → push。PR の base は **dev**
4. dev へ merge → CI が `:dev` image を build/push → 検証環境が自動更新
   (**検証環境を止めている間は CI (test/frontend/e2e) 通過を検証の代替とし、その旨を
   昇格 PR に明記する**)
5. 検証 OK なら**本番昇格**。**pin bump は dev 側で行い、最後に dev → main を 1 回だけ merge する**
   (理由は下記「なぜ pin bump を dev でやるか」):
   1. **dev に `v0.3.x` タグを切る** → CI が `:v0.3.x` を build/push
   2. 配備側マニフェストの image tag を pin bump する PR を出す。**base は `dev`**
   3. **昇格前に `git merge-base --is-ancestor origin/main origin/dev` を確認する**
      (真 = main は dev の祖先 = ff できる。偽なら手順 6 へ)。
   4. dev → main の昇格 PR を出して merge (`git merge-base --is-ancestor` が真であれば
      **fast-forward** になる)
   → 本番が同期 (本番は moving tag を使わず **pin tag** 駆動)

   > タグを切ってから 4. の昇格までの間に dev へ別の変更を merge すると、それも一緒に
   > 本番へ出る。昇格までは dev へのマージを止めるか、止められないなら先に昇格を済ませる。

6. **`git merge-base --is-ancestor origin/main origin/dev` が偽だった場合** (main 側に
   直接マージした commit が dev に無い。緊急 hotfix・インフラ都合の変更などで実際に起きる):
   **先に main → dev の back-merge を済ませてから**手順 5 をやり直す。ff を確認せずに
   `git push origin dev:main` を強行すると non-fast-forward で reject されるか、force push
   すると main 固有のファイル (本番専用のマニフェスト等) が GitOps の prune によって即座に
   本番から削除される。back-merge の結果コンフリクトした場合の解決方針は
   下記「衝突時の解決方針」を参照。

> 検証環境を止めている間は「dev で確認してから」を運用できず、main へ直接マージした commit が
> 積み上がって main が dev より大きく先行する形になりやすい。
> **feature→main の直マージ (hotfix 含む) を行った場合は、次の dev→main 昇格を待たず、
> 速やかに main→dev の back-merge を行うこと。**

**なぜ pin bump を dev でやるか (back-merge 債務の根治)**

以前は「dev → main を merge してから main 側で pin bump」の順だった。この順だと
**pin bump commit が main にしか存在しない**ため、昇格のたびに main が dev より先行し、
back-merge しない限り差が開き続ける。実際に配備側マニフェストの pin が両ブランチで
食い違う事態が起きている。放置すると次の昇格 PR で古い pin が main へ巻き戻る事故につながる。

pin bump を dev 側で行えば、**少なくとも pin bump 単体が原因で main が先行することは無くなる**。
ただし緊急 hotfix やインフラ都合の変更を main へ直接マージした場合は、その時点で main が
dev より先行する。
**この状態を放置しない限り** (= 速やかに back-merge すれば) 昇格は fast-forward になる。
放置すると次の pin bump 時に配備側マニフェストがまとめてコンフリクトし、解決を誤ると
本番 pin が古い値へ巻き戻る。「main は常に dev の部分集合」は自動的に成立する不変条件では
なく、back-merge を都度実施することで維持する運用ルールだと理解すること。

**緊急時 (致命的障害)**

検証を挟む余裕が無い障害対応は、**その旨を明示した上で** main を直接 hotfix してよい。
**hotfix 後は次の通常昇格を待たず、速やかに main を dev へ back-merge**し、両環境のソースを
揃える。back-merge を怠ったまま次の pin bump を dev で行うと、次の昇格 PR で配備側
マニフェストがまとめてコンフリクトする。

**衝突時の解決方針**: dev→main の昇格・main→dev の back-merge のどちらでも、配備側
マニフェストの image pin や本番専用のオーバーレイがコンフリクトしたら、
**main 側 (= 本番で現に稼働している値) を採用する**。dev 側の値は検証環境の都合で
古いことがあるため、pin の巻き戻りを避けるにはこれが安全。

**デプロイ対応表**

| トリガー | CI が作る image | 反映先 |
|---|---|---|
| `dev` への push | `:<sha7>` + `:dev` (moving) | 検証環境 |
| `v*` タグ + 配備側マニフェストの pin bump (**dev**) | `:<sha7>` + `:vX.Y.Z` (pin) | (昇格 merge 後に 本番) |
| `main` への push (昇格 merge / hotfix) | `:<sha7>` のみ | 本番 (pin は昇格 merge が運んでくる) |

## レジストリの保持ポリシー (本番 pin は必ず `v*` タグにする)

コンテナレジストリの保持ポリシー (retention) は「直近 push N 件を保護」「直近 N 日以内に
push されたものを保護」といったルールの OR 結合で組むのが一般的で、そこに
「`v*` タグ限定で直近 N 件を保護」のルールを足すことが多い。

**タグ限定のルールは、その形式のタグを 1 件でも持つ repository にしか効かない。** sha7 の
コミットタグしか無い repository は、push 頻度次第で他のルールの枠から古い順に落ち、
実質無防備になる。

これが原因の本番影響のある事故が起きている:

- 本番が pin していたタグがレジストリから削除され、稼働中の Pod はノードにキャッシュされた
  イメージだけで動き続けていた (v* タグが残っている commit へ pin し直して復旧)。
- sha7 タグしか持たない repository が保持ルールで削除され、そのイメージで動いていた
  定期実行がまとめて停止した。

**本番 pin は必ず `v*` タグを使う** (sha7 やブランチの moving tag を pin にしない)。
pin bump 前には次の両方を確認してから切り替える:

- `crane digest <registry>/<project>/server:vX.Y.Z` — digest が存在すること
- 署名を必須にしている場合は、対応する署名 (`sha256-<digest>.sig` タグ等) が存在すること

確認せずに pin を切り替えると、存在しないイメージを指すか、署名検証のポリシーに拒否される。

この規約は本体だけでなく、同じレジストリ配下のサブシステム各リポジトリにも同様に適用される。
各リポで pin を管理する際も `v*` タグを維持すること。

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
| `detect-secrets` | 機密漏れ | 誤検知なら `detect-secrets audit .secrets.baseline` で is_secret=false に |
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
