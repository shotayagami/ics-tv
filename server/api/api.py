# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ICS-TV JSON API (django-ninja) のルート。

公開フロント (React) / 管理 SPA から呼ぶ JSON API。サーフェス移行は段階的
(strangler-fig): 既存の Django テンプレ/ビューは残したまま、ルータを 1 本ずつ
足していく。認証はブラウザ同一オリジン前提で **既存の session cookie + CSRF を踏襲**
する (JWT/トークン層は持たない)。OpenAPI から TS クライアントを自動生成する
(manage.py dump_openapi → frontend の openapi-typescript)。

mount 分離 (#sec M-4): 公開ホスト tv.* には視聴者島 API だけを出す `public_api` を、内部連携
(M2M) + 管理 SPA を持つ `internal_admin_api` を分けて別 NinjaAPI にする。公開 urlconf
(config.urls_public) は public_api だけをマウントし、内部/管理ルータは構造的に到達不能にする。
管理/内部/放送ホスト (config.urls / config.urls_ops) は両方を api/v1/ にマウントする
(django-ninja は Router を複数 API に共有できないため API 自体を 2 つに割る)。
"""

from ninja import NinjaAPI

from api.routers.admin import router as admin_router
from api.routers.admin_access import router as admin_access_router
from api.routers.admin_billing import router as admin_billing_router
from api.routers.admin_channels import router as admin_channels_router
from api.routers.admin_creators import router as admin_creators_router
from api.routers.admin_graphics import router as admin_graphics_router
from api.routers.admin_live_rundown import router as admin_live_rundown_router
from api.routers.admin_live_sources import router as admin_live_sources_router
from api.routers.admin_medialib import router as admin_medialib_router
from api.routers.admin_ops import router as admin_ops_router
from api.routers.admin_program import router as admin_program_router
from api.routers.admin_sales import router as admin_sales_router
from api.routers.admin_scheduling import router as admin_scheduling_router
from api.routers.admin_series import router as admin_series_router
from api.routers.admin_youtube import router as admin_youtube_router
from api.routers.comments import router as comments_router
from api.routers.detail import router as detail_router
from api.routers.discover import router as discover_router
from api.routers.fanclub_public import router as fanclub_public_router
from api.routers.guide import router as guide_router
from api.routers.health import router as health_router
from api.routers.home import router as home_router
from api.routers.internal import router as internal_router
from api.routers.mobile_auth import router as mobile_auth_router
from api.routers.player import router as player_router
from api.routers.series_public import router as series_public_router

# 認証は既存サイトと同一の session cookie を踏襲する (別トークン層 = JWT は導入しない)。
# django-ninja 1.x はセッション認証 (ninja.security.django_auth) を使う操作で CSRF を自動
# 強制するため、NinjaAPI 側の csrf フラグは不要 (0.x の csrf=True は廃止)。保護が要る操作には
# 個別に auth=django_auth を付ける (Phase 0 の health は未認証 GET なので付けない)。
# --- 公開ホスト tv.* にも出してよい視聴者島 API のみ (#sec M-4) ---
public_api = NinjaAPI(
    title="ICS-TV Public API",
    version="0.1.0",
    description="ICS-TV 公開フロント (視聴者島) 向け JSON API",
    urls_namespace="icstv_api",
)
public_api.add_router("/health", health_router)
# 公開プレイヤー島 (#Phase1)。どちらも /channels 配下 (パス衝突なし)。
public_api.add_router("", player_router)
public_api.add_router("", comments_router)
# 公開番組表島 (#Phase2)。/guide。
public_api.add_router("", guide_router)
# 公開トップ ライブカード島 (#Phase2b)。/home。
public_api.add_router("", home_router)
# 公開ディスカバリ島 (#Phase2c)。/search, /browse, /vod。
public_api.add_router("", discover_router)
# 公開 番組詳細 操作バー島 (#Phase2c)。/program/{id}。
public_api.add_router("", detail_router)
# 公開 シリーズ詳細 API。/series/{id}。番組紹介サイトページ用。
public_api.add_router("", series_public_router)
# ネイティブアプリ トークン認証 (#MOBILE-01)。/auth/*。web の session ログインとは別経路。
public_api.add_router("", mobile_auth_router)
# 公開ファンクラブ島 (#MOBILE-02)。/fc/*, /members/fanclub。アプリの FC 画面用。
public_api.add_router("", fanclub_public_router)

# --- 内部連携 (M2M) + 管理 SPA。公開ホストには一切マウントしない (#sec M-4) ---
internal_admin_api = NinjaAPI(
    title="ICS-TV Internal/Admin API",
    version="0.1.0",
    description="ICS-TV 内部連携 (X-Internal-Token) + 管理 SPA (staff 限定) 向け JSON API",
    urls_namespace="icstv_api_admin",
)
# 内部連携 (#22): 天気予報サブシステム管理コンソール → medialib 取込 等。X-Internal-Token 認証。
internal_admin_api.add_router("", internal_router)
# studio 管理 SPA (#Phase2d)。/admin/*。各 Router 全体に staff_auth (staff 限定)。
internal_admin_api.add_router("", admin_router)
internal_admin_api.add_router("", admin_billing_router)  # #Phase2d-2 請求 (状態遷移)
internal_admin_api.add_router("", admin_scheduling_router)  # #Phase2d-3 編成タイムライン (本丸)
internal_admin_api.add_router("", admin_channels_router)  # #Phase2d-5 チャンネル管理
internal_admin_api.add_router("", admin_live_sources_router)  # 生入力 LiveSource (OBS ingest 閲覧)
internal_admin_api.add_router("", admin_medialib_router)  # #Phase2d-6 素材ライブラリ
internal_admin_api.add_router("", admin_series_router)  # #Phase2d-7 週間編成 series
internal_admin_api.add_router("", admin_ops_router)  # #Phase2d-8 運用 ops ダッシュボード (本丸)
internal_admin_api.add_router("", admin_sales_router)  # #Phase2d-9 営業 CM割付
internal_admin_api.add_router("", admin_youtube_router)  # #Phase2d-12 YouTube スロット dashboard
internal_admin_api.add_router("", admin_graphics_router)  # #Phase2e-1 自動グラフィック graphic_cues
internal_admin_api.add_router("", admin_program_router)  # #Phase2e-2 番組フォーム
internal_admin_api.add_router("", admin_live_rundown_router)  # タイムキープ Phase1 生キューシート
internal_admin_api.add_router(
    "", admin_access_router
)  # アクセス統計 (awstats 的な画面。studio/backoffice 共通)
internal_admin_api.add_router("", admin_creators_router)  # #27 ファンクラブ (Creator/ティア/枠契約)
