# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path

from scheduling import edit_views, graphic_views, views

app_name = "scheduling"

urlpatterns = [
    path("ch/<slug:slug>/timeline/", views.timeline, name="timeline"),
    path("ch/<slug:slug>/programs/new/", views.program_create, name="program_create"),
    path(
        "ch/<slug:slug>/programs/<int:program_id>/edit/",
        views.program_update,
        name="program_update",
    ),
    path(
        "ch/<slug:slug>/programs/<int:program_id>/delete/",
        views.program_delete,
        name="program_delete",
    ),
    # CM枠への CM 手動割当 (キューシートで定義した枠に specific CM を割当)
    path(
        "ch/<slug:slug>/programs/<int:program_id>/breaks/",
        views.program_breaks,
        name="program_breaks",
    ),
    path("adbreaks/<int:adbreak_id>/items/add/", views.adbreak_item_add, name="adbreak_item_add"),
    path(
        "adbreak-items/<int:item_id>/delete/",
        views.adbreak_item_delete,
        name="adbreak_item_delete",
    ),
    # Phase 2 タイムライン編集 API (ドラッグ移動/リサイズ/検証/ad_break)
    path(
        "ch/<slug:slug>/programs/validate/",
        edit_views.program_validate,
        name="program_validate",
    ),
    path(
        "ch/<slug:slug>/programs/<int:program_id>/move/",
        edit_views.program_move,
        name="program_move",
    ),
    path(
        "ch/<slug:slug>/programs/<int:program_id>/resize/",
        edit_views.program_resize,
        name="program_resize",
    ),
    path(
        "ch/<slug:slug>/programs/<int:program_id>/adbreaks/add/",
        edit_views.adbreak_create,
        name="adbreak_create",
    ),
    path(
        "ch/<slug:slug>/programs/<int:program_id>/adbreaks/apply-cuesheet/",
        edit_views.adbreak_apply_cuesheet,
        name="adbreak_apply_cuesheet",
    ),
    path(
        "ch/<slug:slug>/adbreaks/<int:adbreak_id>/move/",
        edit_views.adbreak_update,
        name="adbreak_update",
    ),
    path(
        "ch/<slug:slug>/adbreaks/<int:adbreak_id>/delete/",
        edit_views.adbreak_delete,
        name="adbreak_delete",
    ),
    # 生キューシート (LiveRundown/LiveCue) 編集 (タイムキープ Phase 1 / #25)
    path(
        "ch/<slug:slug>/programs/<int:program_id>/cues/add/",
        edit_views.live_cue_create,
        name="live_cue_create",
    ),
    path(
        "ch/<slug:slug>/cues/<int:cue_id>/update/",
        edit_views.live_cue_update,
        name="live_cue_update",
    ),
    path(
        "ch/<slug:slug>/cues/<int:cue_id>/move/",
        edit_views.live_cue_move,
        name="live_cue_move",
    ),
    path(
        "ch/<slug:slug>/cues/<int:cue_id>/delete/",
        edit_views.live_cue_delete,
        name="live_cue_delete",
    ),
    # 定番進行表 (LiveRundownTemplate/…Cue) 編集 (タイムキープ Phase3 C / #25 §9)
    path(
        "ch/<slug:slug>/slots/<int:slot_id>/template-cues/add/",
        edit_views.template_cue_create,
        name="template_cue_create",
    ),
    path(
        "ch/<slug:slug>/template-cues/<int:cue_id>/update/",
        edit_views.template_cue_update,
        name="template_cue_update",
    ),
    path(
        "ch/<slug:slug>/template-cues/<int:cue_id>/move/",
        edit_views.template_cue_move,
        name="template_cue_move",
    ),
    path(
        "ch/<slug:slug>/template-cues/<int:cue_id>/delete/",
        edit_views.template_cue_delete,
        name="template_cue_delete",
    ),
    # 週間基本編成 (Series / SeriesSlot) UI + 手動展開 (#6 Phase C / Phase 2)
    path("ch/<slug:slug>/series/", views.series_list, name="series_list"),
    path("ch/<slug:slug>/series/expand/", views.series_expand_now, name="series_expand"),
    path("ch/<slug:slug>/series/<int:series_id>/", views.series_edit, name="series_edit"),
    path(
        "ch/<slug:slug>/series/<int:series_id>/delete/", views.series_delete, name="series_delete"
    ),
    path("ch/<slug:slug>/series/<int:series_id>/slots/add/", views.slot_add, name="slot_add"),
    path("ch/<slug:slug>/slots/<int:slot_id>/edit/", views.slot_edit, name="slot_edit"),
    path("ch/<slug:slug>/slots/<int:slot_id>/delete/", views.slot_delete, name="slot_delete"),
    # 週間グリッド (曜日×タイムライン) のスロット D&D 編集 API (JSON, postJson)
    path("ch/<slug:slug>/slots/create/", edit_views.slot_create_grid, name="slot_create_grid"),
    path(
        "ch/<slug:slug>/slots/<int:slot_id>/move/", edit_views.slot_move_grid, name="slot_move_grid"
    ),
    path(
        "ch/<slug:slug>/slots/<int:slot_id>/resize/",
        edit_views.slot_resize_grid,
        name="slot_resize_grid",
    ),
    # 自動グラフィックセット (GraphicCue) 編集 (#18 §C)。owner = series|program|filler。
    path(
        "ch/<slug:slug>/graphic-cues/<str:owner>/<int:owner_id>/",
        graphic_views.graphic_cues,
        name="graphic_cues",
    ),
    path(
        "ch/<slug:slug>/graphic-cues/<str:owner>/<int:owner_id>/add/",
        graphic_views.graphic_cue_add,
        name="graphic_cue_add",
    ),
    path(
        "ch/<slug:slug>/graphic-cues/<str:owner>/<int:owner_id>/cue/<int:cue_id>/delete/",
        graphic_views.graphic_cue_delete,
        name="graphic_cue_delete",
    ),
]
