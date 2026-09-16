# ICS-TV コントロールプレーン (server)

モジュラモノリス（Django 5.1 + Celery）。単一コード/単一PG を **web / beat / worker / normalize**
の複数ワークロードとして k8s に展開する（[../docs/overview.md](../docs/overview.md) 意思決定 #14）。

## アプリ構成（= モジュール境界 = 将来のサービス抽出シーム）

| app | 担当 | 主テーブル |
|-----|------|-----------|
| `core` | チャンネル / 生入力 | channel, live_source |
| `medialib` | 素材 / CM / フィラー | asset, cm_creative, cm_bundle(_item), filler_playlist(_item) |
| `scheduling` | 編成（フリー編成） | program, ad_break(_item) |
| `youtube` | 枠管理 / OAuth | youtube_slot, youtube_credential, youtube_config |
| `playout` | as-run 送出イベント | playout_event |

設計は [../docs/datamodel.md](../docs/datamodel.md) / [../docs/scheduler.md](../docs/scheduler.md)。

## ローカル起動

```
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 専用PG/Redis の接続先を設定
python manage.py migrate      # 専用PostgreSQL 必須（btree_gist は migration が有効化）
python manage.py runserver
```

## ワークロード（同一イメージ、command で切替）

| ワークロード | command |
|---|---|
| web | `gunicorn config.wsgi:application --bind 0.0.0.0:8000` |
| beat | `celery -A config beat -l INFO` |
| worker | `celery -A config worker -Q default -l INFO` |
| normalize | `celery -A config worker -Q normalize -l INFO` |

k8s マニフェストは [../deploy/k8s/](../deploy/k8s/)。

## テスト

開発依存は `requirements-dev.txt` (pytest + pytest-django + pytest-playwright + httpx)、専用 image
`Dockerfile.dev` に Playwright chromium も含めて build 済み。`docker-compose.yml` の
`test` profile から起動する。

```
# Django Client による HTTP view smoke (DB は test 用に自動作成)
docker compose --profile test run --rm test pytest tests/api/test_http_views.py

# Playwright E2E (live_server + chromium headless)
docker compose --profile test run --rm test pytest -m e2e tests/e2e/

# gRPC smoke (docker compose の grpc サービスを起動した状態で)
docker compose up -d postgres grpc
docker compose --profile test run --rm test pytest -m grpc tests/api/test_grpc_smoke.py

# DB 制約テスト (TransactionTestCase)
docker compose --profile test run --rm test python manage.py test scheduling
```

## 注意

- **専用 PostgreSQL**：他サービスと共有の PostgreSQL サーバとは別インスタンス（既存方針）。`program` の重なり禁止に
  `btree_gist` + EXCLUDE を使うため PostgreSQL 必須（SQLite 不可）。
- 機密（YouTube 永続キー / `youtube_credential`）は暗号化保管 TODO（モデルにコメント）。
- Celery タスクは [docs/scheduler.md](../docs/scheduler.md) / [docs/youtube.md](../docs/youtube.md) /
  [docs/overview.md](../docs/overview.md) 3.2 に従って実装済み。AMCP コマンド本体 (caspar.py の
  `amcp()`) は Phase 1 後半。
