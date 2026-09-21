# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""stripe SDK のオブジェクト互換 (stripe>=12 で StripeObject が dict 継承をやめた件)。

v10 当時の StripeObject は dict だったため、ゲートウェイと webhook は Stripe から受け取った
オブジェクトに直接 `.get()` を使っていた。v15 では `AttributeError: get` になり、ティア変更
(アップ/ダウン両方) と webhook が全滅する。本番の Stripe トラフィックが 0 件だったため
2026-08-11 まで発覚しなかった。

既存の test_fanclub_tier_change.py はゲートウェイ関数ごと monkeypatch しているためこの層を
一切通らない。ここでは **実物の StripeObject / Event を通す** ことで回帰を止める。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from django.test import override_settings
from stripe._stripe_object import StripeObject

from core.utils import stripe_to_dict
from fanclub import stripe_gateway as gw
from fanclub import webhook as wh
from fanclub.models import FcProcessedStripeEvent
from subscriptions import stripe_gateway as sub_gw

_SECRET = "whsec_compat_test"  # pragma: allowlist secret - test only


def _obj(data: dict) -> StripeObject:
    return StripeObject.construct_from(data, "sk_test_compat")


def _signed(payload: dict, secret: str) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


class _FakeStripe:
    """_client() の差し替え先。retrieve/create は実物の StripeObject を返す。"""

    def __init__(self, sub: StripeObject, sched: StripeObject | None = None):
        self.sub_modify: list[dict] = []
        self.sched_modify: list[dict] = []
        self.released: list[str] = []
        outer = self

        class Subscription:
            @staticmethod
            def retrieve(_id):
                return sub

            @staticmethod
            def modify(_id, **kw):
                outer.sub_modify.append(kw)

        class SubscriptionSchedule:
            @staticmethod
            def retrieve(_id):
                return sched

            @staticmethod
            def create(**_kw):
                return sched

            @staticmethod
            def modify(_id, **kw):
                outer.sched_modify.append(kw)

            @staticmethod
            def release(_id):
                outer.released.append(_id)

        self.Subscription = Subscription
        self.SubscriptionSchedule = SubscriptionSchedule


# --- SDK の前提そのものを固定する -------------------------------------------------


def test_stripe_object_does_not_support_get():
    """`.get()` が使えないことを明示的に固定する (この前提が崩れたら気付けるように)。"""
    o = _obj({"id": "sub_1"})
    assert o["id"] == "sub_1"
    assert o.id == "sub_1"
    with pytest.raises(AttributeError):
        o.get("id")


def test_stripe_to_dict_converts_nested():
    o = _obj({"id": "sub_1", "items": {"data": [{"id": "si_1", "price": {"id": "price_hi"}}]}})
    d = stripe_to_dict(o)
    assert isinstance(d, dict) and not isinstance(d, StripeObject)
    assert d.get("id") == "sub_1"
    # 入れ子まで plain dict になっていること (多段アクセスが安全になる)
    assert d["items"].get("data")[0]["price"].get("id") == "price_hi"
    # 既に dict のものはそのまま通せる (べき等)
    assert stripe_to_dict({"a": 1}).get("a") == 1


# --- ゲートウェイが実物の StripeObject を受けても動くこと ---------------------------


def test_change_subscription_price_now_accepts_stripe_object(monkeypatch):
    """アップグレード経路。修正前はここで AttributeError: get になっていた。"""
    fake = _FakeStripe(_obj({"id": "sub_1", "items": {"data": [{"id": "si_1"}]}}))
    monkeypatch.setattr(gw, "_client", lambda: fake)

    gw.change_subscription_price_now(subscription_id="sub_1", new_price_id="price_lo")

    assert fake.sub_modify == [
        {"items": [{"id": "si_1", "price": "price_lo"}], "proration_behavior": "always_invoice"}
    ]


def test_schedule_subscription_price_at_period_end_accepts_stripe_object(monkeypatch):
    """ダウングレード経路。修正前はここで AttributeError: get になっていた。"""
    sched = _obj(
        {
            "id": "sub_sched_1",
            "phases": [
                {
                    "start_date": 1_700_000_000,
                    "end_date": 1_800_000_000,
                    "items": [{"price": "price_hi", "quantity": 1}],
                }
            ],
        }
    )
    fake = _FakeStripe(_obj({"id": "sub_1", "schedule": None}), sched)
    monkeypatch.setattr(gw, "_client", lambda: fake)

    effective = gw.schedule_subscription_price_at_period_end(
        subscription_id="sub_1", new_price_id="price_lo"
    )

    assert effective == 1_800_000_000
    assert len(fake.sched_modify) == 1
    call = fake.sched_modify[0]
    assert call["end_behavior"] == "release"
    phases = call["phases"]
    assert len(phases) == 2
    # 現フェーズは現行 Price を維持 (支払い済み期間の権益を取り上げない)
    assert phases[0]["items"] == [{"price": "price_hi", "quantity": 1}]
    assert phases[0]["end_date"] == 1_800_000_000
    # 次フェーズが新 Price
    assert phases[1]["items"] == [{"price": "price_lo", "quantity": 1}]


def test_release_subscription_schedule_accepts_stripe_object(monkeypatch):
    fake = _FakeStripe(_obj({"id": "sub_1", "schedule": "sub_sched_1"}))
    monkeypatch.setattr(gw, "_client", lambda: fake)
    assert gw.release_subscription_schedule(subscription_id="sub_1") is True
    assert fake.released == ["sub_sched_1"]


def test_release_subscription_schedule_is_noop_without_schedule(monkeypatch):
    fake = _FakeStripe(_obj({"id": "sub_1", "schedule": None}))
    monkeypatch.setattr(gw, "_client", lambda: fake)
    assert gw.release_subscription_schedule(subscription_id="sub_1") is False
    assert fake.released == []


# --- webhook が plain dict を受け取ること -----------------------------------------


def _event_payload(event_id: str, etype: str) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": etype,
        "created": int(time.time()),
        "data": {"object": {"id": "cs_1", "object": "checkout.session", "metadata": {}}},
    }


def test_fanclub_construct_event_returns_plain_dict():
    body, sig = _signed(_event_payload("evt_fc_1", "invoice.created"), _SECRET)
    with override_settings(STRIPE_FC_WEBHOOK_SECRET=_SECRET):
        event = gw.construct_event(body, sig)
    assert isinstance(event, dict) and not isinstance(event, StripeObject)
    # 修正前はハンドラ 1 行目のこれが AttributeError になっていた
    assert event.get("id") == "evt_fc_1"
    assert (event.get("data") or {}).get("object", {}).get("id") == "cs_1"


def test_subscriptions_construct_event_returns_plain_dict():
    body, sig = _signed(_event_payload("evt_sub_1", "invoice.created"), _SECRET)
    with override_settings(STRIPE_WEBHOOK_SECRET=_SECRET):
        event = sub_gw.construct_event(body, sig)
    assert isinstance(event, dict) and not isinstance(event, StripeObject)
    assert event.get("id") == "evt_sub_1"


def test_handle_event_processes_constructed_event(db):
    """署名検証から冪等記録まで、実物の Event を通して端から端まで動くこと。"""
    body, sig = _signed(_event_payload("evt_fc_e2e", "invoice.created"), _SECRET)
    with override_settings(STRIPE_FC_WEBHOOK_SECRET=_SECRET):
        event = gw.construct_event(body, sig)

    wh.handle_event(event)  # dispatch 対象外の型なので副作用は冪等記録のみ

    assert FcProcessedStripeEvent.objects.filter(event_id="evt_fc_e2e").exists()
    wh.handle_event(event)  # 再送は冪等にスキップされる
    assert FcProcessedStripeEvent.objects.filter(event_id="evt_fc_e2e").count() == 1
