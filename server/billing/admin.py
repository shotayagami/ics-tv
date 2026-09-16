# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""billing app の Django admin (経理運用)。"""

from __future__ import annotations

from django.contrib import admin

from billing.models import (
    BillingPeriod,
    BroadcastCertificate,
    Invoice,
    InvoiceLine,
    Payment,
)


@admin.register(BillingPeriod)
class BillingPeriodAdmin(admin.ModelAdmin):
    list_display = ("id", "year", "month", "closed_at", "closed_by")
    list_filter = ("year",)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    raw_id_fields = ("spot_order", "sponsorship", "make_good")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "invoice_number",
        "period",
        "contract",
        "status",
        "subtotal",
        "commission_amount",
        "tax_amount",
        "total",
        "issued_at",
    )
    list_filter = ("status", "period")
    search_fields = ("invoice_number",)
    raw_id_fields = ("period", "contract")
    inlines = [InvoiceLineInline]


@admin.register(BroadcastCertificate)
class BroadcastCertificateAdmin(admin.ModelAdmin):
    list_display = ("id", "period", "contract", "created_by", "created_at")
    list_filter = ("period",)
    raw_id_fields = ("period", "contract")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "paid_amount", "paid_at", "method")
    raw_id_fields = ("invoice",)
