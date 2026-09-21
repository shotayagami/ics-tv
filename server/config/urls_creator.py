# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""クリエイター専用ホスト urlconf (#27 ファンクラブ、creator.*)。

core.middleware.HostUrlconfMiddleware が割当。Google招待サインインとセルフサービス画面のみを
公開する (studio/公開サイトの他機能は構造的に到達不能)。urls_public.py の「リテラルパス」方針を
踏襲しつつ、OAuth の redirect_uri 組み立てにのみ reverse() の named path を使う
(fanclub.creator_oauth.build_flow が "creator_oauth_callback" を参照)。
"""

from django.urls import path

from fanclub import creator_portal_views as portal
from fanclub import creator_views

urlpatterns = [
    path("login/", creator_views.login_page, name="creator_login"),
    path("logout/", creator_views.logout_view, name="creator_logout"),
    path("auth/google/start/", creator_views.oauth_start, name="creator_oauth_start"),
    path("auth/google/callback/", creator_views.oauth_callback, name="creator_oauth_callback"),
    path("invite/<str:token>/", creator_views.invite_landing, name="creator_invite_landing"),
    # セルフサービス画面 (#27 PR8)
    path("", portal.dashboard, name="creator_dashboard"),
    path("stripe/connect/", portal.connect_stripe, name="creator_connect_stripe"),
    path("tiers/", portal.tiers, name="creator_tiers"),
    path("tiers/new/", portal.tier_create, name="creator_tier_create"),
    path("tiers/<int:tier_id>/toggle/", portal.tier_toggle_active, name="creator_tier_toggle"),
    path("series/", portal.series_list, name="creator_series_list"),
    path("series/<int:series_id>/posts/", portal.posts_list, name="creator_posts_list"),
    path("series/<int:series_id>/posts/new/", portal.post_new, name="creator_post_new"),
    path(
        "series/<int:series_id>/posts/<int:post_id>/edit/",
        portal.post_edit,
        name="creator_post_edit",
    ),
    path(
        "series/<int:series_id>/posts/<int:post_id>/delete/",
        portal.post_delete,
        name="creator_post_delete",
    ),
    path("members/", portal.members_summary, name="creator_members_summary"),
    path("contracts/", portal.contracts_list, name="creator_contracts_list"),
    path(
        "contracts/<int:contract_id>/checkout/",
        portal.contract_checkout,
        name="creator_contract_checkout",
    ),
    path(
        "contracts/<int:contract_id>/portal/",
        portal.contract_portal,
        name="creator_contract_portal",
    ),
    path("settlements/", portal.settlements_list, name="creator_settlements_list"),
    path("youtube/", portal.youtube_destination, name="creator_youtube_destination"),
    path("profile/", portal.profile_edit, name="creator_profile_edit"),
]
