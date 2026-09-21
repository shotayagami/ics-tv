# ICS-TV frontend (Phase 0 foundation)

Django MVC → React/API 段階移行の土台 (`design/frontend_react_migration_plan.md`)。
**サーフェス移行はまだ行っていない**。これは基盤(workspace/トークン/型クライアント/ビルド配線)のみ。

## 構成 (npm workspaces)

```
frontend/
  packages/api/     OpenAPI 型クライアント (schema.ts / client.ts、openapi-fetch)
  packages/theme/   Bootstrap 5 テーマ SCSS (公開/管理の 2 テーマ)
  apps/web/         公開フロントの React アプリシェル (Vite + TS)
```

## コマンド

```bash
cd frontend
npm install
npm run typecheck         # 全 workspace 型検査
npm run build             # apps/web を dist/ にビルド (Django が /static/web/ で配信)
npm run dev               # Vite 開発サーバ
```

## API 型クライアント生成 (ninja OpenAPI → TS)

```bash
# 1) サーバから OpenAPI を書き出す
cd ../server && .venv/bin/python manage.py dump_openapi --out ../openapi.json
# 2) openapi-typescript で型生成 (packages/api/src/schema.ts)
cd ../frontend && npm run gen:api
```

`gen:api` は生成後に `tools/spdx_headers.py --fix` で `packages/api/src/schema.ts` へ SPDX ヘッダを付けるため、python3 が必要。

`packages/api/src/client.ts` が `openapi-fetch` で型付き同一オリジン呼び出しを提供する。

## Django 連携 (Option B: web イメージ同梱)

`npm run build` の `dist/` を Dockerfile が `server/frontend_dist/` へコピー →
`collectstatic` が `STATICFILES_DIRS [("web", frontend_dist)]` を `STATIC_ROOT/web` へ集約 →
WhiteNoise が `/static/web/` で配信。SPA シェルは `/app/` (`api.spa.spa_index`)。
将来フロント Pod を分離する場合のみ独立イメージ化する (移行計画 Phase 3 以降)。
