# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#6 Phase B B1: billing app モデルの制約テスト (invoice total CHECK / period unique / line one)。"""

from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, transaction

from billing.models import BillingPeriod, Invoice, InvoiceLine, InvoiceStatus, Payment
from sales.models import AdContract, Advertiser, ContractKind, Industry


def _contract():
    ind = Industry.objects.create(code="auto", name="自動車")
    adv = Advertiser.objects.create(name="A社", industry=ind)
    return AdContract.objects.create(
        kind=ContractKind.SPOT,
        advertiser=adv,
        title="C",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
    )


def _period():
    return BillingPeriod.objects.create(year=2026, month=6)


def _invoice(period, contract, subtotal=100000, commission=15000, tax=8500):
    return Invoice.objects.create(
        invoice_number="INV-2026-06-0001",
        period=period,
        contract=contract,
        bill_to_agency=False,
        subtotal=subtotal,
        commission_amount=commission,
        tax_amount=tax,
        total=subtotal - commission + tax,
    )


def test_billing_period_unique(db):
    BillingPeriod.objects.create(year=2026, month=6)
    with pytest.raises(IntegrityError), transaction.atomic():
        BillingPeriod.objects.create(year=2026, month=6)


def test_billing_period_month_check(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        BillingPeriod.objects.create(year=2026, month=13)


def test_invoice_total_check_enforced(db):
    p, c = _period(), _contract()
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(
            invoice_number="INV-X",
            period=p,
            contract=c,
            bill_to_agency=False,
            subtotal=100000,
            commission_amount=15000,
            tax_amount=8500,
            total=999999,  # != subtotal - commission + tax
        )


def test_invoice_valid_and_lines(db):
    p, c = _period(), _contract()
    inv = _invoice(p, c)
    assert inv.total == 100000 - 15000 + 8500
    InvoiceLine.objects.create(
        invoice=inv,
        description="スポットCM 6月分 30本",
        quantity=30,
        unit_price=50000,
        amount=1500000,
    )
    assert inv.lines.count() == 1


def test_invoice_line_adjustment_both_null_ok(db):
    # 調整行 (make_good 精算) は spot_order/sponsorship とも NULL で可 (chk_line_one は「両方」を禁止)
    p, c = _period(), _contract()
    inv = _invoice(p, c)
    InvoiceLine.objects.create(
        invoice=inv, description="前月欠送分減額", quantity=1, unit_price=-50000, amount=-50000
    )
    assert inv.lines.count() == 1


def test_payment_create(db):
    p, c = _period(), _contract()
    inv = _invoice(p, c)
    Payment.objects.create(invoice=inv, paid_amount=93500, paid_at=date(2026, 7, 31), method="振込")
    assert inv.payments.count() == 1
    assert inv.status == InvoiceStatus.DRAFT
