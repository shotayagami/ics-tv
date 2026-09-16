# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材・CM 管理 UI (#7 / docs/ui.md「素材・CM」) の URL。/medialib/ 配下、staff 専用。"""

from django.urls import path

from medialib import views

app_name = "medialib"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("asset/<int:asset_id>/renormalize/", views.op_renormalize, name="renormalize"),
    path("cm/<int:asset_id>/screening/", views.op_screening, name="screening"),
    # キューシート (素材内部ランダウン) 編集
    path("asset/<int:asset_id>/cuesheet/", views.cuesheet_editor, name="cuesheet"),
    path("asset/<int:asset_id>/cuesheet/add/", views.cuepoint_add, name="cuepoint_add"),
    path("cuepoint/<int:point_id>/delete/", views.cuepoint_delete, name="cuepoint_delete"),
    path("cuepoint/<int:point_id>/move/", views.cuepoint_move, name="cuepoint_move"),
    # 専用編集 (admin 直編集の置換)
    path("asset/<int:asset_id>/edit/", views.asset_edit, name="asset_edit"),
    path("asset/<int:asset_id>/preview-url/", views.asset_preview_url, name="asset_preview_url"),
    path("cm/new/", views.cm_new, name="cm_new"),
    path("cm/<int:asset_id>/edit/", views.cm_edit, name="cm_edit"),
    path("bundle/new/", views.bundle_new, name="bundle_new"),
    path("bundle/<int:bundle_id>/edit/", views.bundle_edit, name="bundle_edit"),
    path("bundle/<int:bundle_id>/items/add/", views.bundle_item_add, name="bundle_item_add"),
    path("bundle-item/<int:item_id>/delete/", views.bundle_item_delete, name="bundle_item_delete"),
    path("bundle-item/<int:item_id>/move/", views.bundle_item_move, name="bundle_item_move"),
    path("filler/new/", views.filler_new, name="filler_new"),
    path("filler/<int:playlist_id>/edit/", views.filler_edit, name="filler_edit"),
    path("filler/<int:playlist_id>/items/add/", views.filler_item_add, name="filler_item_add"),
    path("filler-item/<int:item_id>/delete/", views.filler_item_delete, name="filler_item_delete"),
    path("filler-item/<int:item_id>/move/", views.filler_item_move, name="filler_item_move"),
]
