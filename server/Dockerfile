# ICS-TV control plane (modular monolith)。単一イメージを web/beat/worker/normalize/grpc で共用。
# ビルドコンテキスト = リポジトリルート (server/ と frontend/ の双方を含める):
#   docker build -f server/Dockerfile .
# フロント React (Vite) を web イメージへ同梱する (Option B / 移行計画 Phase 0)。

# ---- frontend ビルド (Vite/React) ----
FROM node:24-slim AS frontend
WORKDIR /fe
# 依存解決層: lockfile + 各 workspace の package.json を先に入れ、ソース変更で npm ci 層を壊さない。
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/packages/api/package.json ./packages/api/package.json
COPY frontend/packages/theme/package.json ./packages/theme/package.json
COPY frontend/apps/web/package.json ./apps/web/package.json
COPY frontend/apps/player/package.json ./apps/player/package.json
COPY frontend/apps/guide/package.json ./apps/guide/package.json
COPY frontend/apps/home/package.json ./apps/home/package.json
COPY frontend/apps/discover/package.json ./apps/discover/package.json
COPY frontend/apps/detail/package.json ./apps/detail/package.json
COPY frontend/apps/studio/package.json ./apps/studio/package.json
COPY frontend/apps/ops/package.json ./apps/ops/package.json
COPY frontend/apps/bootstrap-public/package.json ./apps/bootstrap-public/package.json
COPY frontend/apps/bootstrap-admin/package.json ./apps/bootstrap-admin/package.json
RUN npm ci
# ソース投入 → ビルド (apps/web/dist)。TS クライアント schema.ts はコミット済 (Django 不要)。
COPY frontend/ ./
RUN npm run build

# ---- server (Django) ----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ .

# フロント Vite ビルドを同梱 (Option B)。collectstatic が STATIC_ROOT/web へ集約 →
# WhiteNoise が /static/web/ で配信 (Vite base=/static/web/)。SPA シェルは /app/ (api.spa)。
COPY --from=frontend /fe/apps/web/dist ./frontend_dist
# 公開プレイヤー島 (#Phase1) → /static/web/player/player.js (epg.html がマウント)
COPY --from=frontend /fe/apps/player/dist ./frontend_dist/player
# 公開番組表島 (#Phase2) → /static/web/guide/guide.js (guide.html がマウント)
COPY --from=frontend /fe/apps/guide/dist ./frontend_dist/guide
# 公開トップ ライブカード島 (#Phase2b) → /static/web/home/home.js (home.html がマウント)
COPY --from=frontend /fe/apps/home/dist ./frontend_dist/home
# 公開ディスカバリ島 (#Phase2c) → /static/web/discover/discover.js (search/browse/vod.html がマウント)
COPY --from=frontend /fe/apps/discover/dist ./frontend_dist/discover
# 公開 詳細ページ島 (#Phase2c) → /static/web/detail/detail.js (program.html / vod_detail.html がマウント)
COPY --from=frontend /fe/apps/detail/dist ./frontend_dist/detail
# studio 管理 SPA (#Phase2d) → /static/web/studio/studio.js (studio/app.html シェルがマウント)
COPY --from=frontend /fe/apps/studio/dist ./frontend_dist/studio
# 🔴 放送コンソール (リファクタ Phase 1) → /static/web/ops/ops.js (ops/app.html シェルがマウント)
COPY --from=frontend /fe/apps/ops/dist ./frontend_dist/ops
# Bootstrap テーマ束 (移行計画 Phase 0) → /static/web/bootstrap/ (各 SSR シェルが <link>/<script> でロード)。
# public(cyan)/admin(green) の 2 CSS + 各 JS を同一ディレクトリへ集約 (ファイル名が衝突しないため merge 可)。
COPY --from=frontend /fe/apps/bootstrap-public/dist ./frontend_dist/bootstrap
COPY --from=frontend /fe/apps/bootstrap-admin/dist ./frontend_dist/bootstrap

# アプリ版数をビルド時に焼き込む (CI が git タグ/sha を build-arg で渡す → フッタ表示が常に正しい)。
# COPY 後・collectstatic 前に置き、apt/pip 層のキャッシュを版数変更で壊さない。
ARG ICSTV_VERSION=dev
ENV ICSTV_VERSION=${ICSTV_VERSION}

# 静的ファイルを収集 (WhiteNoise が STATIC_ROOT から配信)。DB 不要・SECRET_KEY は default で可。
RUN python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
