# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開 POST /api/v1/forms/{id}/submit — 匿名投書・会員必須・rate-limit・無効フォーム。"""

from __future__ import annotations

import json
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from scheduling.models import AudienceForm, AudienceFormKind, AudienceSubmission, Series


def _series(channel):
    return Series.objects.create(channel=channel, title="Radio Show")


def _form(
    series,
    *,
    kind=AudienceFormKind.MESSAGE,
    enabled=True,
    requires_login=False,
    starts_at=None,
    ends_at=None,
):
    return AudienceForm.objects.create(
        series=series,
        kind=kind,
        title="メッセージを送る",
        enabled=enabled,
        requires_login=requires_login,
        starts_at=starts_at,
        ends_at=ends_at,
        fields=[
            {
                "key": "radio_name",
                "label": "ラジオネーム",
                "type": "text",
                "required": True,
                "options": [],
                "help": "",
            },
            {
                "key": "message",
                "label": "メッセージ",
                "type": "textarea",
                "required": True,
                "options": [],
                "help": "",
            },
        ],
    )


def _member(*, email="user@example.com"):
    from members.models import Member

    m = Member(email=email, nickname="N", birth_year=1990, birth_month=1, postal_code="1000000")
    m.set_password("pw123456")  # pragma: allowlist secret - test only
    m.save()
    return m


def _login(client, member):
    s = client.session
    s["member_id"] = member.pk
    s.save()


def _post(client, form_id, payload):
    return client.post(
        f"/api/v1/forms/{form_id}/submit",
        data=json.dumps({"payload": payload}),
        content_type="application/json",
    )


# ===== 基本ケース =====


def test_message_anonymous_ok(channel, db):
    """投書フォーム (requires_login=False) は匿名で送信できる。"""
    af = _form(_series(channel))
    resp = _post(Client(), af.id, {"radio_name": "匿名リスナー", "message": "こんにちは"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert AudienceSubmission.objects.filter(form=af).count() == 1


def test_message_requires_login_gate(channel, db):
    """requires_login=True で未ログインは gate=login (200 with ok=False)。"""
    af = _form(_series(channel), requires_login=True)
    resp = _post(Client(), af.id, {"radio_name": "X", "message": "hi"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is False
    assert data.get("gate") == "login"


def test_message_requires_login_member_ok(channel, db):
    """requires_login=True でログイン済み会員は送信できる。"""
    af = _form(_series(channel), requires_login=True)
    c = Client()
    _login(c, _member())
    resp = _post(c, af.id, {"radio_name": "会員ラジオ名", "message": "こんにちは"})
    assert resp.status_code == 200


# ===== enabled / campaign =====


def test_disabled_form_404(channel, db):
    """enabled=False のフォームは 404。"""
    af = _form(_series(channel), enabled=False)
    resp = _post(Client(), af.id, {"radio_name": "X", "message": "Y"})
    assert resp.status_code == 404


def test_campaign_expired_410(channel, db):
    """期限切れキャンペーンは 410。"""
    now = timezone.now()
    af = _form(
        _series(channel),
        kind=AudienceFormKind.CAMPAIGN,
        requires_login=True,
        starts_at=now - timedelta(hours=2),
        ends_at=now - timedelta(hours=1),
    )
    c = Client()
    _login(c, _member())
    resp = _post(c, af.id, {"radio_name": "X", "message": "Y"})
    assert resp.status_code == 410


# ===== バリデーション =====


def test_submit_missing_required_422(channel, db):
    af = _form(_series(channel))
    resp = _post(Client(), af.id, {"radio_name": "OK"})  # message 欠落
    assert resp.status_code == 422


# ===== rate-limit =====


def test_rate_limit_cooldown_429(channel, db):
    """同一 IP で 30s 以内の連投は 429。"""
    af = _form(_series(channel))
    c = Client(REMOTE_ADDR="1.2.3.4")

    resp1 = _post(c, af.id, {"radio_name": "RI", "message": "1回目"})
    assert resp1.status_code == 200

    # 同一 IP からすぐ 2 回目 → cooldown (30s) にひっかかる
    resp2 = _post(c, af.id, {"radio_name": "RI", "message": "2回目"})
    assert resp2.status_code == 429
