# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""studio 管理 SPA (#Phase2d-2): 請求 billing の admin API。staff 限定。

状態遷移パターン: 月次締め(draft 生成) → 発行(issue) → 入金(pay)/失効(void)。
既存 billing.views / services を薄く JSON 化する。close-month は警告ありで force でなければ
closed=False+warnings を返し (例外にしない) SPA に force 確認させる。発行/入金/失効の業務エラーは
BillingError → HttpError(409)。各遷移後 SPA はダッシュボードを再取得する。
"""

from __future__ import annotations

from datetime import date

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError

from api.auth import staff_auth
from api.schemas import BillingOut, CloseMonthIn, CloseMonthOut, InvoicePayIn, OkOut

router = Router(tags=["admin"], auth=staff_auth)


@router.get("/admin/billing/dashboard", response=BillingOut)
def billing_dashboard(request: HttpRequest):
    from billing.models import BillingPeriod, Invoice

    periods = [
        {
            "year": p.year,
            "month": p.month,
            "closed_at": p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else "",
        }
        for p in BillingPeriod.objects.order_by("-year", "-month")[:24]
    ]
    invoices = []
    for inv in Invoice.objects.select_related(
        "period", "contract", "contract__advertiser"
    ).order_by("-id")[:100]:
        invoices.append(
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "period": f"{inv.period.year}-{inv.period.month:02d}",
                "advertiser": inv.contract.advertiser.name,
                "subtotal": inv.subtotal,
                "commission_amount": inv.commission_amount,
                "tax_amount": inv.tax_amount,
                "total": inv.total,
                "status": inv.status,
            }
        )
    return {"periods": periods, "invoices": invoices}


@router.post("/admin/billing/close-month", response=CloseMonthOut)
def close_month(request: HttpRequest, payload: CloseMonthIn):
    from billing import services

    try:
        res = services.close_month(
            payload.year, payload.month, force=payload.force, actor=request.user
        )
    except services.BillingError as e:
        raise HttpError(409, str(e)) from e
    if not res["closed"]:
        # 締め前確認: 警告を返す (例外でなく) → SPA が force 再送を促す
        return {"closed": False, "warnings": res["warnings"], "message": "締め前確認"}
    n = len(res["invoice_ids"])
    return {
        "closed": True,
        "warnings": [],
        "message": f"{payload.year}-{payload.month:02d} を締めました (invoice {n} 件)",
    }


def _invoice(invoice_id: int):
    from billing.models import Invoice

    return get_object_or_404(Invoice, pk=invoice_id)


@router.post("/admin/billing/invoices/{int:invoice_id}/issue", response=OkOut)
def issue(request: HttpRequest, invoice_id: int):
    from billing import services

    try:
        services.issue_invoice(_invoice(invoice_id), actor=request.user)
    except services.BillingError as e:
        raise HttpError(409, str(e)) from e
    return {"ok": True}


@router.post("/admin/billing/invoices/{int:invoice_id}/void", response=OkOut)
def void(request: HttpRequest, invoice_id: int):
    from billing import services

    try:
        services.void_invoice(_invoice(invoice_id), actor=request.user)
    except services.BillingError as e:
        raise HttpError(409, str(e)) from e
    return {"ok": True}


@router.post("/admin/billing/invoices/{int:invoice_id}/pay", response=OkOut)
def pay(request: HttpRequest, invoice_id: int, payload: InvoicePayIn):
    from billing import services

    try:
        services.register_payment(
            _invoice(invoice_id),
            payload.paid_amount,
            paid_at=date.today(),
            method=payload.method,
            actor=request.user,
        )
    except services.BillingError as e:
        raise HttpError(409, str(e)) from e
    return {"ok": True}
