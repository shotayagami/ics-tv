# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""請求 UI (#6 Phase B) の URL。/billing/ 配下、staff (経理) 専用。"""

from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("close/", views.op_close_month, name="close_month"),
    path("invoice/<int:invoice_id>/issue/", views.op_issue, name="issue"),
    path("invoice/<int:invoice_id>/void/", views.op_void, name="void"),
    path("invoice/<int:invoice_id>/pay/", views.op_pay, name="pay"),
]
