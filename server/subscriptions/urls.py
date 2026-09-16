# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""サブスク URL (公開ホスト config.urls_public 配下 /subscriptions/)。"""

from django.urls import path

from subscriptions import views

app_name = "subscriptions"

urlpatterns = [
    path("", views.subscription_page, name="page"),
    path("exclusive/", views.exclusive, name="exclusive"),
    path("checkout/<slug:slug>/confirm/", views.checkout_confirm, name="checkout_confirm"),
    path("checkout/<slug:slug>/", views.checkout, name="checkout"),
    path("portal/", views.portal, name="portal"),
    path("stripe/webhook/", views.webhook, name="webhook"),
]
