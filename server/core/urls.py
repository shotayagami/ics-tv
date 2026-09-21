# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""core (運用 + 公開) の URL。home はルートに置く (config/urls.py 経由)。

ops/  → 運用画面 (緊急 SLATE 等、認証想定)
public/ → 視聴者向け公開ページ (認証不要)
"""

from django.urls import path

from core import admin_views, ops_views, views

app_name = "core"

# 🔴 放送当直の送出操作エンドポイント (POST)。studio.* フル urlconf と ops.* 放送ホスト
# (config.urls_ops) の双方が共有する (リファクタ Phase 1.2)。放送コンソール (React) は
# /ops/ch/<slug>/... へ form-POST する。プレフィックス (ops/) が一意なので urlpatterns 内の
# 順序には依存しない。ここを唯一の正本にして studio.*/ops.* でパス定義がドリフトしないようにする。
OPS_OPERATIONS = [
    path("ops/ch/<slug:slug>/slate/", views.emergency_slate, name="slate"),
    path("ops/ch/<slug:slug>/clear-slate/", ops_views.op_clear_slate, name="op_clear_slate"),
    path("ops/ch/<slug:slug>/reload-main/", ops_views.op_reload_main, name="op_reload_main"),
    path(
        "ops/ch/<slug:slug>/auto-return/",
        ops_views.op_auto_return_toggle,
        name="op_auto_return_toggle",
    ),
    path("ops/ch/<slug:slug>/cm-in/", ops_views.op_cm_in, name="op_cm_in"),
    path("ops/ch/<slug:slug>/cm-return/", ops_views.op_cm_return, name="op_cm_return"),
    path("ops/ch/<slug:slug>/overlay/", ops_views.op_overlay, name="op_overlay"),  # #18 §B
    path(
        "ops/ch/<slug:slug>/notifications/<int:pk>/ack/",
        ops_views.op_ack_notification,
        name="op_ack_notification",
    ),
    path(
        "ops/ch/<slug:slug>/notifications/ack-all/",
        ops_views.op_ack_all_notifications,
        name="op_ack_all_notifications",
    ),
    # 押え / 巻き (#7 O-C)。生番組の尺調整 (放送コンソールからも操作)。
    path(
        "ops/ch/<slug:slug>/program/<int:program_id>/extend/",
        ops_views.op_extend,
        name="op_extend",
    ),
    path(
        "ops/ch/<slug:slug>/program/<int:program_id>/shorten/",
        ops_views.op_shorten,
        name="op_shorten",
    ),
    # 配信枠 (YT) の testing/live/complete 遷移 (P1.3・放送コンソールから go-live)。
    path(
        "ops/ch/<slug:slug>/slot/<int:slot_id>/transition/",
        ops_views.op_slot_transition,
        name="op_slot_transition",
    ),
    # 生キューシート (タイムキープ Phase 1 / #25): CM cue 発火 / VT 送出 / cue スキップ。
    path("ops/ch/<slug:slug>/cue/<int:cue_id>/cm-now/", ops_views.op_cm_now, name="op_cm_now"),
    path("ops/ch/<slug:slug>/cue/<int:cue_id>/roll-vt/", ops_views.op_roll_vt, name="op_roll_vt"),
    path("ops/ch/<slug:slug>/cue/<int:cue_id>/skip/", ops_views.op_skip_cue, name="op_skip_cue"),
]

urlpatterns = [
    # 送出操作 (放送当直・studio.*/ops.* 共有)。
    *OPS_OPERATIONS,
    path("public/ch/<slug:slug>/", views.public_epg, name="public_epg"),
    path("t/<path:key>", views.thumb_serve, name="thumb"),  # R2 サムネ配信 (#7)
    # ライブ静止画 ingest (送出ノード→管理ホスト/LAN, token 認証, #7 ライブ表示)。
    # 公開 urlconf には載せない (公開ホスト=視聴専用の原則。write は管理ホストのみ)。
    path(
        "internal/live-poster/<slug:slug>/",
        views.live_poster_ingest,
        name="live_poster_ingest",
    ),
    # 運行ダッシュボード (#7 O-B, staff)
    path("ops/ch/<slug:slug>/dashboard/", ops_views.ops_dashboard, name="ops_dashboard"),
    # 視聴計測ダッシュボード (#ADMIN-02)
    path("ops/ch/<slug:slug>/analytics/", ops_views.ops_analytics, name="ops_analytics"),
    path(
        "ops/ch/<slug:slug>/panel/concurrent/",
        ops_views.panel_concurrent,
        name="ops_panel_concurrent",
    ),
    path("ops/ch/<slug:slug>/panel/now/", ops_views.panel_now_playing, name="ops_panel_now"),
    path(
        "ops/ch/<slug:slug>/panel/health/",
        ops_views.panel_health,
        name="ops_panel_health",
    ),
    path("ops/ch/<slug:slug>/panel/asrun/", ops_views.panel_asrun, name="ops_panel_asrun"),
    path(
        "ops/ch/<slug:slug>/cut-live-return/",
        ops_views.op_cut_live_return,
        name="op_cut_live_return",
    ),
    # 通知 3 層 (#7 O-B)
    path(
        "ops/ch/<slug:slug>/panel/notifications/",
        ops_views.panel_notifications,
        name="ops_panel_notifications",
    ),
    path(
        "ops/ch/<slug:slug>/notifications/",
        ops_views.notification_center,
        name="notification_center",
    ),
    # 押え / 巻き (#7 O-C)。extend/shorten は OPS_OPERATIONS へ移動 (studio.*/ops.* 共有)。
    # preview (確認用 HTML 断片) は studio 専用に据え置き (放送コンソールは window.confirm を使う)。
    path(
        "ops/ch/<slug:slug>/program/<int:program_id>/extend-preview/",
        ops_views.op_extend_preview,
        name="op_extend_preview",
    ),
    # admin UI (staff_member_required)
    # チャンネル名 / slug 編集の専用画面 (全ch一覧)。<slug:slug>/ より前に置き先に解決させる。
    path(
        "admin-ui/channels/",
        admin_views.channel_list,
        name="channel_list",
    ),
    path(
        "admin-ui/channels/<slug:slug>/update/",
        admin_views.channel_update,
        name="channel_update",
    ),
    path(
        "admin-ui/ch/<slug:slug>/",
        admin_views.channel_settings,
        name="channel_settings",
    ),
    path(
        "admin-ui/ch/<slug:slug>/youtube/connect/",
        admin_views.youtube_oauth_start,
        name="youtube_oauth_start",
    ),
    path(
        "admin-ui/oauth/callback/",
        admin_views.youtube_oauth_callback,
        name="youtube_oauth_callback",
    ),
    path(
        "admin-ui/ch/<slug:slug>/youtube/livestream/create/",
        admin_views.create_persistent_stream_view,
        name="create_persistent_stream",
    ),
    # YouTube 配信メニュー (#23 で「設定」から分離した統合入口)
    path(
        "admin-ui/ch/<slug:slug>/youtube/",
        admin_views.youtube_console,
        name="youtube_console",
    ),
    path(
        "admin-ui/ch/<slug:slug>/youtube/slots/",
        admin_views.slot_dashboard,
        name="slot_dashboard",
    ),
    # 配信プリセット CRUD (#23)
    path("admin-ui/youtube/presets/", admin_views.preset_list, name="preset_list"),
    path("admin-ui/youtube/presets/new/", admin_views.preset_new, name="preset_new"),
    path(
        "admin-ui/youtube/presets/<int:preset_id>/edit/",
        admin_views.preset_edit,
        name="preset_edit",
    ),
    path(
        "admin-ui/youtube/presets/<int:preset_id>/delete/",
        admin_views.preset_delete,
        name="preset_delete",
    ),
    # #23 番組専用枠 ダッシュボード + 手動操作
    path(
        "admin-ui/ch/<slug:slug>/youtube/dedicated/",
        admin_views.program_broadcast_dashboard,
        name="program_broadcast_dashboard",
    ),
    path(
        "admin-ui/youtube/dedicated/<int:program_id>/create/",
        admin_views.program_broadcast_create_now,
        name="program_broadcast_create_now",
    ),
    path(
        "admin-ui/youtube/program-broadcast/<int:pb_id>/transition/",
        admin_views.program_broadcast_transition,
        name="program_broadcast_transition",
    ),
    path(
        "admin-ui/youtube/program-broadcast/<int:pb_id>/checklist/",
        admin_views.program_broadcast_checklist,
        name="program_broadcast_checklist",
    ),
    path(
        "admin-ui/youtube/program-broadcast/<int:pb_id>/delete/",
        admin_views.program_broadcast_delete,
        name="program_broadcast_delete",
    ),
    path(
        "admin-ui/ch/<slug:slug>/cloudflare/live-input/create/",
        admin_views.create_cf_live_input_view,
        name="create_cf_live_input",
    ),
    path(
        "admin-ui/ch/<slug:slug>/cloudflare/playback-url/",
        admin_views.set_cf_playback_url,
        name="set_cf_playback_url",
    ),
    path(
        "admin-ui/ch/<slug:slug>/media/",
        admin_views.set_channel_media,
        name="set_channel_media",
    ),
    path(
        "admin-ui/ch/<slug:slug>/clock-overlay/",
        admin_views.set_clock_overlay,
        name="set_clock_overlay",
    ),
    path(
        "admin-ui/ch/<slug:slug>/broadcast-windows/",
        admin_views.set_broadcast_windows,
        name="set_broadcast_windows",
    ),
    path(
        "admin-ui/ch/<slug:slug>/clock-style/",
        admin_views.set_clock_style,
        name="set_clock_style",
    ),
    path(
        "admin-ui/chime/sound/",
        admin_views.upload_chime_sound,
        name="upload_chime_sound",
    ),
    path(
        "admin-ui/chime/sound/<int:sound_id>/delete/",
        admin_views.delete_chime_sound,
        name="delete_chime_sound",
    ),
    path(
        "admin-ui/chime/sound/<int:sound_id>/preview/",
        admin_views.preview_chime_sound,
        name="preview_chime_sound",
    ),
    path(
        "admin-ui/ch/<slug:slug>/chime/select/",
        admin_views.select_channel_chime,
        name="select_channel_chime",
    ),
    path(
        "admin-ui/ch/<slug:slug>/cloudflare/live-output/create/",
        admin_views.create_cf_live_output_view,
        name="create_cf_live_output",
    ),
    path(
        "admin-ui/slot/<int:slot_id>/transition/",
        admin_views.slot_transition_view,
        name="slot_transition",
    ),
    path(
        "admin-ui/slot/<int:slot_id>/delete/",
        admin_views.slot_delete_view,
        name="slot_delete",
    ),
    path(
        "admin-ui/slot/<int:slot_id>/meta/",
        admin_views.slot_meta_update_view,
        name="slot_meta_update",
    ),
    path(
        "admin-ui/slot/<int:slot_id>/apply-template/",
        admin_views.slot_apply_template_view,
        name="slot_apply_template",
    ),
]
