# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""有料サービスの法定表示: 特定商取引法に基づく表記 + 規約/プライバシーの課金記載。"""

from __future__ import annotations

from django.test import override_settings

from subscriptions.models import Plan

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])


@_PUBLIC_HOST
def test_tokushoho_page_renders(http_client, db):
    res = http_client.get("/tokushoho/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert "特定商取引法に基づく表記" in body
    assert "支払方法" in body and "解約" in body and "返金" in body


@_PUBLIC_HOST
def test_tokushoho_lists_plan_price(http_client, db):
    Plan.objects.create(name="松", slug="matsu", amount=1280, rank=3)
    body = http_client.get("/tokushoho/").content.decode("utf-8")
    assert "1280円" in body


@_PUBLIC_HOST
def test_terms_has_paid_subscription_section(http_client, db):
    body = http_client.get("/terms/").content.decode("utf-8")
    assert "有料サブスクリプション" in body and "自動更新" in body


@_PUBLIC_HOST
def test_privacy_mentions_payment_data(http_client, db):
    body = http_client.get("/privacy/").content.decode("utf-8")
    assert "決済情報" in body and "Stripe" in body


@_PUBLIC_HOST
def test_footer_has_tokushoho_link(http_client, db):
    # 任意の公開ページのフッタに特商法リンク
    body = http_client.get("/terms/").content.decode("utf-8")
    assert "/tokushoho/" in body
