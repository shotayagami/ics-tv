# 利用・運用ガイド (URL・ログイン・起動)

「どこにアクセスし、どうログインし、何ができるか」をまとめた実務ガイド。設計の意図は
[overview.md](overview.md) ほか各設計書を参照。`<slug>` は channel のスラッグ (例 `ch1` / デモ `demo1`)。

## 1. アクセス先 (ホスト)

`core.middleware.HostUrlconfMiddleware` が `DJANGO_ADMIN_HOSTS` 以外のホストを公開専用 urlconf
(`config.urls_public`) に切替える。公開ホストには管理ルートが**存在しない (404)** = 構造的に非露出 (#7)。

| 用途 | 内部 (Technitium DNS) | 外部 (Cloudflare Tunnel) |
|------|------|------|
| **公開 (視聴者向け)** | `https://tv.<内部ドメイン>` | `https://tv.yagamin.net` |
| **管理 (フル機能)** | `https://studio.<内部ドメイン>` | `https://studio.yagamin.net` (**CF Access** で保護) |
| **放送コンソール** (🔴 ops SPA・リファクタ Phase 1) | `https://ops.<内部ドメイン>` | `https://ops.yagamin.net` (**CF Access**) |
| **クリエイターポータル** (#27・ファンクラブ) | `https://creator.<内部ドメイン>` | `https://creator.yagamin.net` (Google 招待サインイン) |
| Remotion Studio (レンダプレビュー。Django 外の別 Deployment) | `https://remotion.<内部ドメイン>` | `https://remotion.yagamin.net` (**CF Access**) |
| **dev** | 公開 `dev-tv.<内部ドメイン>` / 管理 `dev-studio.<内部ドメイン>` (= ADMIN_HOSTS) | 公開 `dev-tv.yagamin.net` / 管理 `dev-studio.yagamin.net` ほか (5 系統 dev-tv/dev-studio/dev-deliver/dev-ops/dev-creator の内外。**正本は `deploy/k8s/overlays/dev/patch-ingress.yaml` と `patch-configmap.yaml`**) |
| **ローカル** | `http://localhost:8000` (compose、全機能) | — |

`<内部ドメイン>` は導入者が自分の内部 DNS (Technitium DNS に限らず split-horizon DNS 全般) に
用意するドメインのプレースホルダ。上表の各サブドメイン (`tv.` `studio.` 等) は役割を表す接頭辞
なのでそのまま流用してよい。

外部公開 (`*.yagamin.net`) の**実体はこのリポには無い** — 運用者のインフラ管理リポジトリ (`homelab-infra`) の
`cloudflared/tunnel-config.yaml` + `scripts/cloudflared-ensure-dns.sh` / `-access.sh` が
Tunnel ルート・DNS・CF Access を管理する (ICSTV 系は slidecast/backoffice 等も含め 13 ホスト。
remotion の dev 版が無いのは意図的)。**ホストを追加するときは icstv 側 Ingress と
homelab-infra の両方を直す**こと。

> **dev は 2026-08-26 以降アプリ層 (web/worker/beat/captions/normalize/offload/grpc/
> remotion-studio) が `replicas: 0` で停止中**(上記 dev URL はすべて 503)。ArgoCD の
> sync/health は Synced/Healthy のままなので、この表以外では気づけない。再開手順は
> `deploy/k8s/overlays/dev/kustomization.yaml` 末尾のコメントを参照。

> - **公開ホスト** (`tv.*`): 視聴者向けのみ (`/` チャンネル一覧・`/guide/`・`/ch/<slug>/`・`/t/...`)。管理は 404。
> - **管理ホスト** (`DJANGO_ADMIN_HOSTS` = `studio.<内部ドメイン>,studio.yagamin.net,...`): 編成/運用/営業/
>   請求/admin (納品は別ホスト・別リポ `icstv-delivery`)。`studio.yagamin.net` は外部 Tunnel +
>   **CF Access (Zero Trust)** + Django staff の二重防御。
> - **CF Access** (`studio.yagamin.net`): 許可 = メールドメイン nekomin.jp / yagamin.net / circle-ics.com /
>   eqwel.co.jp / whatsapp.co.jp (Access app `ICS-TV Studio`)。通過後さらに `/admin/login/` で staff 認証。
> - cert は letsencrypt-prod の **DNS01 (Cloudflare, zone yagamin.net)** で全 host を発行 (内部限定ホストも可)。
> - fail-safe: 未知ホストは公開 (制限) 側に倒れる。

## 2. 画面と URL (ロール別)

**公開ホスト (誰でも・認証不要)** — 外部 `tv.yagamin.net` 等:

| 画面 | URL |
|------|-----|
| 公開トップ (チャンネル一覧) | `/` |
| 横断番組表 | `/guide/` (`?date=YYYY-MM-DD`) |
| チャンネル視聴 (プレイヤー + 番組表) | `/ch/<slug>/` (旧 `/public/ch/<slug>/` も可。`?view=week` / `?view=day&date=`) |
| サムネ配信 | `/t/thumbnails/<key>` |

**管理ホスト (`DJANGO_ADMIN_HOSTS` のみ)** — `studio.<内部ドメイン>` (内部) / `studio.yagamin.net` (外部・CF Access):

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
> `/delivery/` は 404。実装は別リポ `icstv-delivery` (社内限定 `deliver-new.<内部ドメイン>`)
> へ移管済み ([docs/delivery.md](delivery.md) は当時の設計記録として歴史的に残置)。
>
> 編成/運用/営業/請求/admin は **staff_member_required / login_required** で保護 (#7。
> 以前の「編成は認証なし=社内NW前提」は解消)。さらに公開ホストではこれらのルート自体が 404。
> **公開ページのプレイヤー (優先順)**: ① channel 設定の **CF HLS 再生 URL** → hls.js → ② LIVE な YouTube 枠埋め込み
> → ③ 「準備中」。本番公開 URL = `https://tv.yagamin.net/`。サムネはアプリ経由 `/t/thumbnails/<key>` 配信。

> **注 (編成タイムライン)**: 現状アプリ層の認証デコレータが無い (社内ネットワーク内アクセス前提)。
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
`frontend/README.md`。手順は CI (`.gitea/workflows/ci.yaml` の `e2e` job) が実行しているものと同一):

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

送出ノード (自宅 Proxmox LXC) の構築・検証は `deploy/playout-node/README.md`。
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
- CI = Gitea Actions (`.gitea/workflows/ci.yaml`)。`test` (ruff/mypy/pytest) 以外に
  `frontend` (npm typecheck+build)・`e2e` (Playwright)・`pip-audit --strict`・
  `detect-secrets` も走る。**`pull_request` イベントでは `build-push` は走らない**
  (`push`/タグ push 限定)。Harbor へ publish されるタグと本番昇格の運用は
  `CONTRIBUTING.md` の「デプロイ対応表」「Harbor retention」を正本とする (ここでは重複させない)。
- push すると `build-push` job が Harbor へ push した image に **cosign 署名**を付ける
  (`.gitea/workflows/ci.yaml` にインライン実装。共通ワークフローへの委譲ではない)。
  repo Secret `COSIGN_KEY` / `COSIGN_PASSWORD` が未設定だと**署名ステップは exit 0 で
  スキップされ CI は緑のまま無署名イメージが push される**ため、「CI が緑=デプロイ可能」
  ではない。署名の有無は Harbor 側 (`crane digest` と対応する `sha256-<digest>.sig` タグ)
  で確認する。

## 8. デプロイ (k8s)

`deploy/k8s/` (kustomize)。本番 = `overlays/production`、dev = `overlays/dev` (ArgoCD、
**2026-08-26 以降アプリ層停止中。§1 参照**)。機密は SealedSecret (`scripts/seal-secrets.sh`。
生成手順・キー一覧・既知の欠落は `deploy/k8s/README.md` が正本)。dev は
`50-seed-demo-job.yaml` が seed_demo を PostSync で流す。本番は **cosign 署名が無いと
Kyverno に拒否される** (デプロイの必須条件。詳細は `../README.md` の「イメージ署名」節)。
リリース昇格の runbook (Harbor 判定基準・imagePullPolicy の含意) は
[operations.md](operations.md) の「リリース昇格 runbook」節を参照。

## 9. トラブルシュート

- **staff 画面に入れない**: ユーザが `is_staff` か確認 (`/admin/`)。納品関連は別リポ
  `icstv-delivery` (本体の `DeliveryAccount` は撤去済み)。
- **編成変更が送出に反映されない**: resolve は 5 分周期。即時反映は編集 API が `resolve_channel_now`
  を発火するが、agent への伝搬は SubscribeEvents (5s polling) 経由。
- **CI の build-push が落ちる**: Harbor/ランナーの一過性が多い。Gitea UI で当該 run を **Re-run**
  (test が success なら build-push のみ再実行で復旧)。Gitea 自体の 503/pack 破損は GC/再起動で復旧。
- **YouTube 枠が「準備中」**: `youtube_slot` 未生成 or error。`/admin-ui/ch/<slug>/youtube/slots/`
  で状態確認、OAuth 連携 (`/admin-ui/ch/<slug>/youtube/connect/`) と永続 liveStream を確認。
