# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase B B3: invoice 発行/失効/入金消込 + 請求 UI。R2 はモック、WeasyPrint 不在で HTML 保存。"""

from __future__ import annotations

from datetime import date

import pytest
from django.test import Client, override_settings

from billing import services
from billing.models import BillingPeriod, Invoice, InvoiceStatus
from billing.services import BillingError, issue_invoice, register_payment, void_invoice
from sales.models import AdContract, Advertiser, ContractKind, Industry


@pytest.fixture
def fake_r2(monkeypatch):
    stored = {}

    class _Client:
        def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
            stored[Key] = (Body, ContentType)

    monkeypatch.setattr(services.r2, "client", lambda: _Client())
    monkeypatch.setattr(services.r2, "bucket", lambda: "b")
    return stored


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


def test_issue_invoice_sets_issued_and_pdf(db, fake_r2):
    inv = _invoice()
    issue_invoice(inv)
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.ISSUED
    assert inv.issued_at is not None
    assert inv.pdf_r2_key == "billing/invoice/INV-2026-06-0001.pdf"
    assert inv.pdf_r2_key in fake_r2  # R2 に保存された


def test_issue_only_from_draft(db, fake_r2):
    inv = _invoice(status=InvoiceStatus.ISSUED)
    with pytest.raises(BillingError):
        issue_invoice(inv)


def test_void_from_issued(db):
    inv = _invoice(status=InvoiceStatus.ISSUED)
    void_invoice(inv)
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.VOID


def test_payment_exact_marks_paid(db):
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    status = register_payment(inv, 110000, paid_at=date(2026, 7, 31))
    assert status == InvoiceStatus.PAID


def test_payment_within_tolerance_marks_paid(db, settings):
    settings.ICSTV_PAYMENT_TOLERANCE = 1000
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    register_payment(inv, 109500, paid_at=date(2026, 7, 31))  # 不足 500 <= 1000
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.PAID


def test_payment_short_stays_issued(db, settings):
    settings.ICSTV_PAYMENT_TOLERANCE = 1000
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    register_payment(inv, 100000, paid_at=date(2026, 7, 31))  # 不足 10000 > 1000
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.ISSUED


# ---- UI ----


def test_billing_dashboard_requires_staff(http_client, db):
    assert http_client.get("/billing/").status_code == 302


def test_billing_dashboard_renders(staff_client, db):
    inv = _invoice()
    res = staff_client.get("/billing/")
    assert res.status_code == 200
    assert inv.invoice_number in res.content.decode("utf-8")


def test_close_month_view_warns_then_force(staff_client, channel, db):
    # 当月未返送が無ければ即締め。ここでは空でも締められる (invoice 0 件)
    res = staff_client.post("/billing/close/", {"year": 2026, "month": 3, "force": "1"})
    assert res.status_code == 200
    assert BillingPeriod.objects.filter(year=2026, month=3, closed_at__isnull=False).exists()


def test_dashboard_month_totals_match_backoffice_seam(staff_client, db, settings):
    """/billing/ の月次合計が読み取り seam と同じ数になる (D038)。

    広告売上の月次合計を出す画面が無料ツリーに無く、集計は seam
    (/internal/backoffice/billing) 1 本だけだった。seam を正として画面をそれに合わせる。

    次の 5 つの壊れ方を 1 本で捕まえる。
    - 1 枚の請求書に 2 件の入金 → invoice と payment を同じ queryset で集約すると
      JOIN が掛け合わさり請求額が 110000 ぶん水増しされる
    - **同額の請求書 2 枚** → 上の水増しを `distinct=True` で消そうとすると、今度は金額が
      同じ請求書が 1 枚に畳まれて 220000 ぶん足りなくなる (水増しより気付きにくい)
    - 入金後に失効した請求書 → 請求額・入金額の両方から落ちる (片方だけだとずれる)
    - 未発行 (draft) の請求書 → seam は void しか除外しないので請求額に含める
    - 別の期の請求書 → 期ごとでなく全期の合計を出すと、両方の行が同じ数になる
    """
    settings.ICSTV_PAYMENT_TOLERANCE = 0
    a = _invoice(status=InvoiceStatus.ISSUED)  # 110000
    period = a.period

    def _more(number, subtotal, tax, status):
        return Invoice.objects.create(
            invoice_number=number,
            period=period,
            contract=a.contract,
            bill_to_agency=False,
            subtotal=subtotal,
            commission_amount=0,
            tax_amount=tax,
            total=subtotal + tax,
            status=status,
        )

    _more("INV-2026-06-0002", 200000, 20000, InvoiceStatus.DRAFT)  # 220000、未発行
    _more("INV-2026-06-0004", 200000, 20000, InvoiceStatus.ISSUED)  # 220000、上と同額
    voided = _more("INV-2026-06-0003", 70000, 7000, InvoiceStatus.ISSUED)  # 77000
    register_payment(a, 50000, paid_at=date(2026, 7, 1))
    register_payment(a, 30000, paid_at=date(2026, 7, 2))  # 1 枚に 2 入金
    register_payment(voided, 77000, paid_at=date(2026, 7, 3))
    void_invoice(voided)  # 入金後に失効
    other = BillingPeriod.objects.create(year=2026, month=5)
    Invoice.objects.create(
        invoice_number="INV-2026-05-0001",
        period=other,
        contract=a.contract,
        bill_to_agency=False,
        subtotal=900000,
        commission_amount=0,
        tax_amount=90000,
        total=990000,
        status=InvoiceStatus.ISSUED,
    )

    with override_settings(BACKOFFICE_READ_TOKEN="bo-tok"):  # pragma: allowlist secret - test only
        seam = (
            Client()
            .get(
                "/api/v1/internal/backoffice/billing?period=2026-06",
                headers={"X-Internal-Token": "bo-tok"},
            )
            .json()
        )
    # 110000 + 220000 + 220000 (77000 は失効ぶん除外) / 50000 + 30000 (失効ぶんの 77000 は除外)
    assert (seam["invoiced_total"], seam["paid_total"], seam["outstanding_total"]) == (
        550000,
        80000,
        470000,
    )

    body = staff_client.get("/billing/").content.decode("utf-8")
    assert f"<td>¥{seam['invoiced_total']}</td>" in body
    assert f"<td>¥{seam['paid_total']}</td>" in body
    assert f"<td>¥{seam['outstanding_total']}</td>" in body
    assert "<td>¥990000</td>" in body  # 2026-05 は別行として出る
