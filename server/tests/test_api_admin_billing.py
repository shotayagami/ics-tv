# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-2): 請求 billing の admin API (状態遷移)。

staff_auth ゲート + ダッシュボード + 月次締め/発行/失効/入金。invoice 生成は test_billing_flow.py
と同じ直接作成パターン。発行は PDF 生成があるため R2 をモックする。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from billing import services
from billing.models import BillingPeriod, Invoice, InvoiceStatus
from sales.models import AdContract, Advertiser, ContractKind, Industry

_DASH = "/api/v1/admin/billing/dashboard"


@pytest.fixture
def fake_r2(monkeypatch):
    class _Client:
        def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
            pass

    monkeypatch.setattr(services.r2, "client", lambda: _Client())
    monkeypatch.setattr(services.r2, "bucket", lambda: "b")


def _invoice(status=InvoiceStatus.DRAFT, total=110000):
    ind = Industry.objects.create(code="auto", name="auto")
    adv = Advertiser.objects.create(name="A社", industry=ind)
    c = AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
        title="C",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )
    p = BillingPeriod.objects.create(year=2026, month=6)
    return Invoice.objects.create(
        invoice_number="INV-2026-06-0001",
        period=p,
        contract=c,
        bill_to_agency=False,
        subtotal=100000,
        commission_amount=0,
        tax_amount=10000,
        total=total,
        status=status,
    )


def _post(client, url, payload=None):
    return client.post(url, data=json.dumps(payload or {}), content_type="application/json")


# ---- 認可 ----


def test_dashboard_requires_auth(http_client, db):
    assert http_client.get(_DASH).status_code == 401


def test_issue_requires_auth(http_client, db):
    assert _post(http_client, "/api/v1/admin/billing/invoices/1/issue").status_code in (401, 403)


# ---- ダッシュボード ----


def test_dashboard_lists_invoice(staff_client, db):
    inv = _invoice()
    d = staff_client.get(_DASH).json()
    row = next(i for i in d["invoices"] if i["id"] == inv.id)
    assert row["invoice_number"] == "INV-2026-06-0001" and row["status"] == "draft"
    assert row["advertiser"] == "A社" and row["total"] == 110000 and row["period"] == "2026-06"


# ---- 状態遷移 ----


def test_issue(staff_client, db, fake_r2):
    inv = _invoice()
    r = _post(staff_client, f"/api/v1/admin/billing/invoices/{inv.id}/issue")
    assert r.status_code == 200 and r.json()["ok"] is True
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.ISSUED


def test_issue_already_issued_returns_409(staff_client, db, fake_r2):
    inv = _invoice(status=InvoiceStatus.ISSUED)
    r = _post(staff_client, f"/api/v1/admin/billing/invoices/{inv.id}/issue")
    assert r.status_code == 409  # BillingError → 409


def test_void(staff_client, db):
    inv = _invoice(status=InvoiceStatus.ISSUED)
    r = _post(staff_client, f"/api/v1/admin/billing/invoices/{inv.id}/void")
    assert r.status_code == 200
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.VOID


def test_pay_full_marks_paid(staff_client, db):
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    r = _post(staff_client, f"/api/v1/admin/billing/invoices/{inv.id}/pay", {"paid_amount": 110000})
    assert r.status_code == 200
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.PAID


# ---- 月次締め ----


def test_close_month_force_empty(staff_client, channel, db):
    # 空の月でも force で締められる (invoice 0 件)。
    r = _post(
        staff_client, "/api/v1/admin/billing/close-month", {"year": 2026, "month": 3, "force": True}
    )
    assert r.status_code == 200 and r.json()["closed"] is True
    assert BillingPeriod.objects.filter(year=2026, month=3, closed_at__isnull=False).exists()
