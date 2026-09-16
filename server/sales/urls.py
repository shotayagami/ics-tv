# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""営業 UI (#6 / ui-sales.md) の URL。/sales/ 配下、staff (営業/管理者) 専用。"""

from django.urls import path

from sales import views

app_name = "sales"

urlpatterns = [
    path("allocation/", views.allocation_view, name="allocation"),
    path("allocation/refill/", views.op_refill, name="refill"),
    path("items/<int:item_id>/swap/", views.op_swap_item, name="swap"),
    path("orders/<int:spot_order_id>/bands/", views.band_editor, name="band_editor"),
    path("orders/<int:spot_order_id>/bands/add/", views.op_add_band, name="add_band"),
    path("bands/<int:band_id>/delete/", views.op_delete_band, name="delete_band"),
    path("orders/<int:spot_order_id>/programs/add/", views.op_add_program, name="add_program"),
    path("programs/<int:sop_id>/delete/", views.op_delete_program, name="delete_program"),
]
