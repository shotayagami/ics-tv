# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開ホスト専用 urlconf (#7 公開/管理分離)。

tv.yagamin.net 等の公開ホストはこの urlconf で動く
(core.middleware.HostUrlconfMiddleware が割当)。視聴者向けページのみを公開し、
編成/運用/営業/請求/納品/admin のルートは存在しない (=404、構造的に露出しない)。
公開テンプレートはリテラルパス (/、/guide/、/ch/<slug>/) を使うので reverse 名前空間に依存しない。
"""

from django.urls import include, path

from api.api import public_api
from api.spa import spa_index
from core import views as core_views
from fanclub import views as fanclub_views

urlpatterns = [
    path("", core_views.public_home, name="public_home"),
    # JSON API (django-ninja)。公開フロント (React) が呼ぶ視聴者島 API のみ。内部連携 (M2M) と
    # 管理 SPA は internal_admin_api に分離済で、公開ホストには一切マウントしない (#sec M-4)。
    path("api/v1/", public_api.urls),
    # フロント React アプリ (Phase 0 / Option B)。SPA シェル + クライアントルーティング。
    path("app/", spa_index, name="spa"),
    path("app/<path:rest>", spa_index),
    path("guide/", core_views.public_guide, name="public_guide"),
    path("guide/week/", core_views.public_guide_week, name="public_guide_week"),
    # 番組詳細 (#EPG-01): EPG/検索からの着地・状態別CTA
    path("program/<int:program_id>/", core_views.public_program_detail, name="public_program"),
    # 検索 (#DISC-02): 番組名/あらすじ/出演者/ジャンル横断
    path("search/", core_views.public_search, name="public_search"),
    # ジャンル別ブラウズ (#DISC-01): 発見導線
    path("browse/", core_views.public_browse, name="public_browse"),
    # シリーズ詳細 / 番組紹介サイト: 1シリーズ1ページ + 記事/キャンペーン投稿
    # <int:series_id> が数字のみにマッチするため英字 slug は後段にフォール (後方互換)
    path("series/<int:series_id>/", core_views.public_series_detail, name="public_series"),
    path(
        "series/<int:series_id>/p/<int:post_id>/",
        core_views.public_series_post,
        name="public_series_post",
    ),
    path("series/<slug:slug>/", core_views.public_series_detail_by_slug, name="public_series_slug"),
    # 見逃し配信 (VOD): 放送済み録画番組のオンデマンド (#VOD-01)
    path("vod/", core_views.public_vod_list, name="public_vod"),
    path("vod/<int:program_id>/", core_views.public_vod_detail, name="public_vod_detail"),
    # 見逃し字幕 VTT (#PLAYER-04): 同一オリジン配信 (<track> の CORS 回避)・視聴ゲート付き
    path(
        "vod/<int:program_id>/captions.vtt",
        core_views.public_vod_captions,
        name="public_vod_captions",
    ),
    path("privacy/", core_views.privacy_policy, name="privacy"),
    path("terms/", core_views.terms_of_service, name="terms"),
    path("tokushoho/", core_views.tokushoho, name="tokushoho"),
    # 視聴者会員 (登録/ログイン/プロフィール/本人確認)。公開ホスト限定 (#会員管理)
    path("members/", include("members.urls")),
    # 視聴者サブスク課金 (プラン/Checkout/Portal/Stripe Webhook)。公開ホスト限定 (#サブスク)
    path("subscriptions/", include("subscriptions.urls")),
    # ファンクラブ 参加/退会 (無料ティア)。公開ホスト限定 (#27)
    path("fanclub/", include("fanclub.urls")),
    # クリエイター公開ファンクラブページ (#27 §10.2)
    path("fc/<slug:slug>/", fanclub_views.creator_page, name="public_fc_page"),
    path("ch/<slug:slug>/", core_views.public_epg, name="public_channel"),
    # 後方互換 (旧公開 URL)
    path("public/ch/<slug:slug>/", core_views.public_epg),
    # サムネ配信 (R2 → アプリ経由)
    path("t/<path:key>", core_views.thumb_serve, name="thumb"),
    # いま放送中: ライブ静止画配信 + トップ自動更新 JSON (#7 ライブ表示)
    path("live/<slug:slug>/poster.jpg", core_views.live_poster_serve, name="live_poster"),
    path("api/now", core_views.now_json, name="now_json"),
    # 視聴ハートビート (#ADMIN-02 視聴計測)
    path("api/beat", core_views.beat, name="beat"),
]
