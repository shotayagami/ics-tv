# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
from django.urls import path

from fanclub import views

app_name = "fanclub"

urlpatterns = [
    path("stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
    path("<slug:creator_slug>/tokushoho/", views.tokushoho, name="tokushoho"),
    path("<slug:creator_slug>/join/", views.join, name="join"),
    path("<slug:creator_slug>/leave/", views.leave, name="leave"),
    path(
        "<slug:creator_slug>/tiers/<int:tier_id>/confirm/",
        views.checkout_confirm,
        name="checkout_confirm",
    ),
    path("<slug:creator_slug>/tiers/<int:tier_id>/checkout/", views.checkout, name="checkout"),
    path(
        "<slug:creator_slug>/tiers/<int:tier_id>/change/confirm/",
        views.change_confirm,
        name="change_confirm",
    ),
    path("<slug:creator_slug>/tiers/<int:tier_id>/change/", views.change, name="change"),
    path("<slug:creator_slug>/change/cancel/", views.change_cancel, name="change_cancel"),
    path("<slug:creator_slug>/chat/post/", views.chat_post, name="chat_post"),
    path(
        "<slug:creator_slug>/chat/<int:message_id>/delete/",
        views.chat_delete,
        name="chat_delete",
    ),
    path("<slug:creator_slug>/portal/", views.portal, name="portal"),
]
