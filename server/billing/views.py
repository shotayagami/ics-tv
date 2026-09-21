# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""請求 UI (#6 Phase B / ui-sales.md 請求)。staff (経理) 専用。月次締め・発行・失効・入金。"""

from __future__ import annotations

from datetime import date

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from billing import services
from billing.models import BillingPeriod, Invoice, InvoiceStatus


def _ok(message: str, trigger: str) -> HttpResponse:
    return HttpResponse(message, headers={"HX-Trigger": trigger})


@staff_member_required
def dashboard(request) -> HttpResponse:
    # 期ごとの請求額・入金額・未収を添える。下の請求書一覧は期をまたいだ直近 100 件なので、
    # 合計をそこから作ると 100 件を超えた期で必ず狂う。合計は期ごとに独立して数える。
    period_rows = services.period_summaries(
        list(BillingPeriod.objects.order_by("-year", "-month")[:24])
    )
    invoices = list(
        Invoice.objects.select_related("period", "contract", "contract__advertiser").order_by(
            "-id"
        )[:100]
    )
    return render(
        request,
        "billing/dashboard.html",
        {"period_rows": period_rows, "invoices": invoices, "statuses": InvoiceStatus.choices},
    )


@staff_member_required
@require_POST
def op_close_month(request) -> HttpResponse:
    """月次締め。warnings ありで force でなければ警告を返す (確認後 force=1 で再 POST)。"""
    try:
        year = int(request.POST["year"])
        month = int(request.POST["month"])
    except (KeyError, ValueError):
        return HttpResponse("year/month が不正です", status=400)
    force = request.POST.get("force") in ("1", "true", "on")
    try:
        res = services.close_month(year, month, force=force, actor=request.user)
    except services.BillingError as e:
        return HttpResponse(str(e), status=409)
    if not res["closed"]:
        return HttpResponse(
            "締め前確認: " + " / ".join(res["warnings"]) + "（force=1 で続行）", status=409
        )
    return _ok(
        f"{year}-{month:02d} を締めました (invoice {len(res['invoice_ids'])} 件)", "monthClosed"
    )


@staff_member_required
@require_POST
def op_issue(request, invoice_id: int) -> HttpResponse:
    invoice = get_object_or_404(Invoice, pk=invoice_id)
    try:
        services.issue_invoice(invoice, actor=request.user)
    except services.BillingError as e:
        return HttpResponse(str(e), status=409)
    return _ok(f"{invoice.invoice_number} を発行しました", "invoiceIssued")


@staff_member_required
@require_POST
def op_void(request, invoice_id: int) -> HttpResponse:
    invoice = get_object_or_404(Invoice, pk=invoice_id)
    try:
        services.void_invoice(invoice, actor=request.user)
    except services.BillingError as e:
        return HttpResponse(str(e), status=409)
    return _ok(f"{invoice.invoice_number} を失効しました", "invoiceVoided")


@staff_member_required
@require_POST
def op_pay(request, invoice_id: int) -> HttpResponse:
    invoice = get_object_or_404(Invoice, pk=invoice_id)
    try:
        amount = int(request.POST["paid_amount"])
    except (KeyError, ValueError):
        return HttpResponse("paid_amount が不正です", status=400)
    try:
        status = services.register_payment(
            invoice,
            amount,
            paid_at=date.today(),
            method=request.POST.get("method", ""),
            actor=request.user,
        )
    except services.BillingError as e:
        return HttpResponse(str(e), status=409)
    return _ok(f"入金登録しました (status={status})", "paymentRegistered")
