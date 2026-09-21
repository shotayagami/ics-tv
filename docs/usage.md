# 利用・運用ガイド (URL・ログイン・起動)

「どこにアクセスし、どうログインし、何ができるか」をまとめた実務ガイド。設計の意図は
[overview.md](overview.md) ほか各設計書を参照。`<slug>` は channel のスラッグ (例 `ch1` / デモ `demo1`)。

## 1. アクセス先 (ホスト)

`core.middleware.HostUrlconfMiddleware` が `DJANGO_ADMIN_HOSTS` 以外のホストを公開専用 urlconf
(`config.urls_public`) に切替える。公開ホストには管理ルートが**存在しない (404)** = 構造的に非露出 (#7)。

| 用途 | 内部 (split-horizon DNS) | 外部 (Cloudflare Tunnel) |
|------|------|------|
| **公開 (視聴者向け)** | `https://tv.<内部ドメイン>` | `https://tv.<公開ドメイン>` |
| **管理 (フル機能)** | `https://studio.<内部ドメイン>` | `https://studio.<公開ドメイン>` (**CF Access** で保護) |
| **放送コンソール** (🔴 ops SPA・リファクタ Phase 1) | `https://ops.<内部ドメイン>` | `https://ops.<公開ドメイン>` (**CF Access**) |
| **クリエイターポータル** (#27・ファンクラブ) | `https://creator.<内部ドメイン>` | `https://creator.<公開ドメイン>` (Google 招待サインイン) |
| **dev** | 公開 `dev-tv.<内部ドメイン>` / 管理 `dev-studio.<内部ドメイン>` (= ADMIN_HOSTS) | 公開 `dev-tv.<公開ドメイン>` / 管理 `dev-studio.<公開ドメイン>` ほか (5 系統 dev-tv/dev-studio/dev-deliver/dev-ops/dev-creator の内外。**ホスト一覧の正本は導入者の配備基盤側 (このリポジトリの範囲外)**) |
| **ローカル** | `http://localhost:8000` (compose、全機能) | — |

`<内部ドメイン>` は導入者が内部 DNS (split-horizon DNS) に用意するドメイン、`<公開ドメイン>` は
外部公開に使うドメインのプレースホルダ。上表の各サブドメイン (`tv.` `studio.` 等) は役割を表す
接頭辞なのでそのまま流用してよい。

外部公開 (`*.<公開ドメイン>`) の**実体はこのリポには無い** — Tunnel ルート・DNS・CF Access は
導入者の配備基盤側 (このリポジトリの範囲外) で管理する。**ホストを追加するときは
配備基盤側の Ingress と Tunnel/DNS/CF Access の両方を直す**こと (片方だけでは外から到達できない)。

> **検証環境を止める運用を採る場合** (dev のアプリ層 — web/worker/beat/captions/normalize/
> offload/grpc — を `replicas: 0` にする)、**その間 dev の URL はすべて 503
> になる**。GitOps コントローラの表示 (sync/health) は正常のままなので、
> 同期状態を見ても止めていることには気づけない。停止・再開の手順は配備基盤側 (このリポジトリの範囲外) に
> 書き残しておくこと。

> - **公開ホスト** (`tv.*`): 視聴者向けのみ (`/` チャンネル一覧・`/guide/`・`/ch/<slug>/`・`/t/...`)。管理は 404。
> - **管理ホスト** (`DJANGO_ADMIN_HOSTS` = `studio.<内部ドメイン>,studio.<公開ドメイン>,...`): 編成/運用/営業/
>   請求/admin (納品は別ホスト・別リポ `icstv-delivery`)。`studio.<公開ドメイン>` は外部 Tunnel +
>   **CF Access (Zero Trust)** + Django staff の二重防御。
> - **CF Access** (`studio.<公開ドメイン>`): 通過を許す認証済みメールドメインは導入者が決める
>   (Access app `ICS-TV Studio`)。通過後さらに `/admin/login/` で staff 認証。
> - cert は letsencrypt-prod の **DNS01 (Cloudflare、zone は導入者の公開ドメイン)** で全 host を発行 (内部限定ホストも可)。
> - fail-safe: 未知ホストは公開 (制限) 側に倒れる。

## 2. 画面と URL (ロール別)

**公開ホスト (誰でも・認証不要)** — 外部 `tv.<公開ドメイン>` 等:

| 画面 | URL |
|------|-----|
| 公開トップ (チャンネル一覧) | `/` |
| 横断番組表 | `/guide/` (`?date=YYYY-MM-DD`) |
| チャンネル視聴 (プレイヤー + 番組表) | `/ch/<slug>/` (旧 `/public/ch/<slug>/` も可。`?view=week` / `?view=day&date=`) |
| サムネ配信 | `/t/thumbnails/<key>` |

**管理ホスト (`DJANGO_ADMIN_HOSTS` のみ)** — `studio.<内部ドメイン>` (内部) / `studio.<公開ドメイン>` (外部・CF Access):

| 画面 | URL | 認証 |
|------|-----|------|
| studio SPA トップ | `/` → `/studio/` へリダイレクト | staff |
| **編成タイムライン** (ドラッグ移動/リサイズ/ad_break 編集) | `/scheduling/ch/<slug>/timeline/` | staff |
| **週間基本編成** (Series/Slot・変則パターン・展開) | `/scheduling/ch/<slug>/series/` | staff |
| **素材・CM 管理** (在庫/考査/正規化/キューシート/サムネ) | `/medialib/` | staff |
| **運行ダッシュボード** (送出監視・押え/巻き・CM-IN・通知) | `/ops/ch/<slug>/dashboard/` | staff |
| **営業: 割付ビュー** | `/sales/allocation/` | staff |
| 営業: 線引きエディタ | `/sales/orders/<id>/bands/` | staff |
| **請求** (月次締め・請求書発行・入金) | `/billing/` | staff |
| YouTube / Cloudflare 設定 | `/admin-ui/ch/<slug>/` | staff |
| YouTube 枠ダッシュボード (枠のタイトル/キャプション編集) | `/admin-ui/ch/<slug>/youtube/slots/` | staff |
| Django 管理 (マスタ CRUD) | `/admin/` | staff/superuser |

> **納品ポータルは撤去済み**。本体の `delivery` app は models/urls を持たない ghost で
> `/delivery/` は 404。実装は別リポ `icstv-delivery` (内部限定 `deliver-new.<内部ドメイン>`)
> へ移管済み ([docs/delivery.md](delivery.md) は当時の設計記録として歴史的に残置)。
>
> 編成/運用/営業/請求/admin は **staff_member_required / login_required** で保護 (#7。
> 以前の「編成は認証なし=内部ネットワーク前提」は解消)。さらに公開ホストではこれらのルート自体が 404。
> **公開ページのプレイヤー (優先順)**: ① channel 設定の **CF HLS 再生 URL** → hls.js → ② LIVE な YouTube 枠埋め込み
> → ③ 「準備中」。本番公開 URL = `https://tv.<公開ドメイン>/`。サムネはアプリ経由 `/t/thumbnails/<key>` 配信。

> **注 (編成タイムライン)**: 現状アプリ層の認証デコレータが無い (内部ネットワーク内アクセス前提)。
> 公開ホストには出さないこと。アプリ認証の付与は今後の課題。

主要な操作系 URL (画面から HTMX/fetch で叩かれる。直接叩く必要は通常なし):
`/scheduling/ch/<slug>/programs/{new,<id>/edit,<id>/move,<id>/resize,validate}`,
`/scheduling/ch/<slug>/adbreaks/...`, `/ops/ch/<slug>/{slate,cm-in,reload-main,...}`,
`/billing/invoice/<id>/{issue,pay,void}`。納品系の操作 URL は別リポ `icstv-delivery` 側。

## 3. ログイン・権限

- **staff 画面** (運行・営業・請求・YouTube設定・admin): Django ユーザの `is_staff=True` が必要。
  ログインは `/admin/login/` (各 staff 画面は未ログイン時ここへリダイレクト)。
- **納品ポータル**: 本体からは撤去済み (`DeliveryAccount` / `DeliveryInvitation` は
  migration 0006 で DROP 済)。認証・権限は別リポ `icstv-delivery` の実装を参照。
- ユーザ/権限の付与は Django admin (`/admin/`) で行う。

## 4. ローカルで起動して触る

`docker compose` の web/beat/worker/normalize/grpc は `./server:/app` をバインドマウントする
ため、イメージ内蔵の `frontend_dist`/`staticfiles` がホスト側 (未ビルドだと空) で隠れる。
**先にフロントをビルドして `server/frontend_dist` へ配置する**こと (詳細は
`frontend/README.md`。手順は CI (`.github/workflows/ci.yml` の `e2e` job) が実行しているものと同一):

```bash
cd frontend && npm ci && npm run build && cd ..
mkdir -p server/frontend_dist/{player,guide,home,discover,detail,studio,ops,bootstrap}
cp -r frontend/apps/web/dist/.              server/frontend_dist/
cp -r frontend/apps/player/dist/.           server/frontend_dist/player/
cp -r frontend/apps/guide/dist/.            server/frontend_dist/guide/
cp -r frontend/apps/home/dist/.             server/frontend_dist/home/
cp -r frontend/apps/discover/dist/.         server/frontend_dist/discover/
cp -r frontend/apps/detail/dist/.           server/frontend_dist/detail/
cp -r frontend/apps/studio/dist/.           server/frontend_dist/studio/
cp -r frontend/apps/ops/dist/.              server/frontend_dist/ops/
cp -r frontend/apps/bootstrap-public/dist/. server/frontend_dist/bootstrap/
cp -r frontend/apps/bootstrap-admin/dist/.  server/frontend_dist/bootstrap/
```

未ビルドのまま起動すると `/studio/` 等の SPA 系画面は空白、`/app/` は 503
(`frontend not built`) になる (SSR 画面の Bootstrap テーマも無スタイルになる)。

```bash
# 1. 起動 (postgres / redis / web:8000 / beat / worker / normalize / grpc:50051)
docker compose up -d

# 2. マイグレーション
docker compose run --rm web python manage.py migrate

# 3. デモデータ投入 (運行/編成/公開/営業/請求が実データで埋まる。冪等)
#    DEBUG=True 環境専用 (compose の web は DJANGO_DEBUG=true 済)。DEBUG=False で実行すると
#    CommandError で拒否される (既知パスワードの superuser を本番に作らないためのガード)。
docker compose run --rm web python manage.py seed_demo
#   → demo / demo12345 (superuser) が作られる。納品アカウントは作られない (納品は別リポ)

# 4. ブラウザで
#   http://localhost:8000/                          → /studio/ へリダイレクト (要ログイン)
#   http://localhost:8000/admin/login/             demo / demo12345 でログイン
#   http://localhost:8000/scheduling/ch/demo1/timeline/   編成
#   http://localhost:8000/ops/ch/demo1/dashboard/         運行
#   http://localhost:8000/ch/demo1/                       公開番組表・視聴
#   http://localhost:8000/sales/allocation/  /billing/
```

`seed_demo --reset` でデモ一式を作り直す。`createsuperuser` で本物の管理者を作る場合:
`docker compose run --rm web python manage.py createsuperuser`。

## 5. 定期実行タスク (Celery beat)

beat コンテナが下記を自動実行する (`config/settings.py` の `CELERY_BEAT_SCHEDULE`)。

| タスク | 周期 | 役割 |
|--------|------|------|
| `scheduling.tasks.resolve_all_channels` | 5分 | 編成 → playout_event 解決 (窓 48h) |
| `scheduling.tasks.ingest_live_recordings` | 1分 | 生放送録画の取り込み (VOD 化) |
| `scheduling.tasks.expand_series_slots` | 週次 | 週間基本編成を Program へ展開 (4週先まで) |
| `youtube.tasks.generate_slots_all` | 10分 | YouTube **4h枠** rolling 生成 |
| `youtube.tasks.rotate_slots_all` | 1分 | 枠の live/complete 遷移 |
| `youtube.tasks.nudge_next_slot_all` | 1分 | 次枠への視聴継続誘導 (ライブチャット+説明欄) |
| `youtube.tasks.generate_dedicated_broadcasts_all` | 5分 | #23 番組専用枠の生成 |
| `youtube.tasks.rotate_dedicated_all` | 1分 | #23 専用枠の遷移 |
| `playout.tasks.check_agent_liveness` | 1分 | agent heartbeat 途絶検知 → 通知 |
| `playout.tasks.check_stuck_slate` | 1分 | スレート固着検知 → 通知 |
| `sales.tasks.reconcile_airings` | 日次 | 放確台帳の取りこぼし回収 |
| `sales.tasks.detect_missed_airings` | 毎時 | 欠送検知 → make_good 起票 |
| `medialib.tasks.reconcile_stale_normalize` | 30分 | 正規化 stale 回収 (取りこぼし救済) |
| `medialib.tasks.reconcile_normalize_offload` | 60秒 | 正規化 Windows オフロードの取り込み/フォールバック |
| `members.tasks.send_due_reminders` | 5分 | 視聴予約リマインドメール |
| `analytics.tasks.prune_stale_presence` | 5分 | 視聴在席 (ViewerPresence) の古い行を掃除 |
| `analytics.tasks.prune_access_log` | 6時間 | アクセスログ retention (35日) |
| `fanclub.tasks.reconcile_creator_youtube_outputs` | 10秒 | クリエイター個人 YouTube 中継の on/off (#27) |

## 6. 送出ノード (CasparCG agent) の立ち上げ

送出ノード (Proxmox LXC) の構築・検証は `deploy/playout-node/README.md`。
要点: `scripts/install.sh` で導入 → `/etc/icstv/agent-<slug>.env` (チャンネルごと。汎用の
`/etc/icstv/agent.env` は旧世代の残骸で unit からは読まれない) に `ICSTV_AGENT_TOKEN`
(= channel.agent_token、admin で生成) 等を設定 → `scripts/validate.sh` で Gate 1-10 を段階検証
(Gate 6 = CG テンプレ スモーク、Gate 9 = HLS ABR、Gate 10 = 送出ヘルス監視)。AMCP/CG の単体
検証は `scripts/amcp-smoke.py`。

本番送出ノードは **Proxmox ホスト上の LXC 1 台**のみ。ch1 は `icstv-agent@ch1` /
`icstv-encoder@ch1` / `icstv-hls-ladder@ch1` / `icstv-poster@ch1` とも `enabled` で、
冷起動から自動復帰する (チャンネル増設時は `@ch2` 等を同様に enable。手順は
`deploy/playout-node/README.md`)。

### 送出開始 (go-live) の流れ

経路 (CasparCG → encoder → CF Live Input → Live Output → YouTube) は構築済み。実放送開始は以下:

1. **実素材を入れて ch1 を編成する** (`/medialib/` で納品→正規化、`/scheduling/ch/ch1/{timeline,series}/` で配置)。
   ← これが最大の前提。素材 0 件では出すものが無い。
2. **送出ノードの `/etc/icstv/agent-ch1.env` に ch1 の `ICSTV_AGENT_TOKEN`** (= channel.agent_token、`/admin/` で生成) を投入。
3. **送出を有効化**: 送出ノードで
   `systemctl enable --now icstv-agent@ch1 icstv-encoder@ch1 icstv-hls-ladder@ch1 icstv-poster@ch1`
   (ladder = 公開プレイヤーの画質切替用 HLS、poster = 公開トップのライブサムネ)。
   = 公開放送の開始。YouTube 枠は `rotate_slots` が壁時計で自動 live 遷移する。
   (チャンネル増設時は `@ch2`/`@ch3` を同様に enable。手順は `deploy/playout-node/README.md`。)
4. **公開プレイヤー用に CF 再生 URL を設定**: `/admin-ui/ch/ch1/` の「視聴者向け HLS 再生 URL」に
   CF Live Input の HLS (.m3u8) を貼付 → 公開ページの CF プレイヤーが有効化。

### YouTube 枠のタイトル/キャプション

`generate_slots` が各 4h 枠の **タイトル/説明をその窓の公開番組から自動生成**する。
個別調整は `/admin-ui/ch/<slug>/youtube/slots/` の各枠「✎ 編集」から (保存で `liveBroadcasts.update` により
YouTube へ即反映)。手動編集した枠は ✎ 表示になり、自動再生成では上書きされない。

## 7. テスト / CI

- ローカルテストは `CONTRIBUTING.md` 参照:
  `docker compose --profile test run --rm test pytest -m "not grpc and not e2e"` (通常)、
  `-m e2e tests/e2e/` (Playwright)、`cd agent && .venv/bin/python -m pytest` (agent)。
- CI = GitHub Actions (`.github/workflows/ci.yml`)。**秘密を一切使わず、イメージの push や署名は行わない。**
  `main` への push と `main` 宛ての pull request で、次の 5 ジョブが走る:
  `test` (pre-commit のフック・Django の system check・migration の差分・OpenAPI と型の生成物の鮮度・
  proto 生成物の server/agent 一致・SPDX ヘッダ・mypy・pytest)、`agent` (pytest)、
  `audit` (`pip-audit --strict`)、`frontend` (typecheck・build・テスト)、`e2e` (Playwright)。
- 以下は**開発側の配備パイプラインの記述で、このツリーの CI には含まれない**
  (開発側の CI はコンテナレジストリへのイメージの push と署名も行う。タグと本番昇格の運用は
  `CONTRIBUTING.md` の「デプロイ対応表」「レジストリの保持ポリシー」を参照)。
  開発側では、push すると `build-push` job がコンテナレジストリへ push した image に
  **イメージ署名**を付ける (開発側の CI 定義にインライン実装。共通ワークフローへの委譲ではない)。
  署名鍵を渡す Secret が未設定だと**署名ステップは exit 0 でスキップされ CI は緑のまま
  無署名イメージが push される**ため、「CI が緑=デプロイ可能」ではない。署名の有無は
  レジストリ側 (`crane digest` と対応する `sha256-<digest>.sig` タグ) で確認する。

## 8. デプロイ (k8s)

k8s へ載せるためのマニフェスト一式は**導入者の配備基盤側 (このリポジトリの範囲外)**。
本番と検証環境をオーバーレイで分け、GitOps コントローラで同期する構成を前提に書いている
(**検証環境を止めたときの挙動は §1 参照**)。機密は Secret として配る (キー一覧の正本は
`server/.env.example`。方針は `CONTRIBUTING.md` の「環境変数 (.env) の共用」節)。検証環境では
同期後のフックで `seed_demo` を流すとデモデータ入りで立ち上がる。**イメージ署名が無いと
ポリシーエンジンに拒否される**構成を採る場合の注意は `../README.md` の「イメージ署名」節。
リリース昇格の runbook (レジストリ側の判定基準・imagePullPolicy の含意) は
[operations.md](operations.md) の「リリース昇格 runbook」節を参照。

## 9. トラブルシュート

- **staff 画面に入れない**: ユーザが `is_staff` か確認 (`/admin/`)。納品関連は別リポ
  `icstv-delivery` (本体の `DeliveryAccount` は撤去済み)。
- **編成変更が送出に反映されない**: resolve は 5 分周期。即時反映は編集 API が `resolve_channel_now`
  を発火するが、agent への伝搬は SubscribeEvents (5s polling) 経由。
- **CI の build-push が落ちる** (開発側の CI の記述。このツリーの CI に `build-push` は無い):
  コンテナレジストリやランナーの一過性の失敗が多い。開発側の CI で当該 run を **Re-run**
  (test が success なら build-push のみ再実行で復旧)。Git ホスティング側の 503 や pack 破損は、
  そちらの GC・再起動で復旧する。
- **YouTube 枠が「準備中」**: `youtube_slot` 未生成 or error。`/admin-ui/ch/<slug>/youtube/slots/`
  で状態確認、OAuth 連携 (`/admin-ui/ch/<slug>/youtube/connect/`) と永続 liveStream を確認。
