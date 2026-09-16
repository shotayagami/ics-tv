# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""営放サブシステム billing app (#6 / docs/sales.md billing)。月次締め・請求書・放送確認書・入金。

billing は確定データ (airing/contract) を read-only 参照する性質で、最も抽出されやすい境界 (S5)。
billed=true の airing と issued 済み invoice は不変 (締めの不変性)。billing → sales の FK は持つ。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "draft"
    ISSUED = "issued", "issued"
    PAID = "paid", "paid"
    VOID = "void", "void"


class BillingPeriod(models.Model):
    """月次締め単位。closed_at NULL = 未締め。"""

    year = models.IntegerField()
    month = models.IntegerField()
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "billing_period"
        constraints = [
            models.UniqueConstraint(fields=["year", "month"], name="uq_billing_period"),
            models.CheckConstraint(
                name="chk_billing_month", condition=Q(month__gte=1) & Q(month__lte=12)
            ),
        ]

    def __str__(self):
        return f"{self.year}-{self.month:02d}"


class Invoice(models.Model):
    """請求書 (S3: インボイス制度対応)。total = subtotal − commission + tax (CHECK 強制)。"""

    invoice_number = models.CharField(max_length=40, unique=True)  # 例 INV-2026-06-0001
    period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name="invoices")
    contract = models.ForeignKey(
        "sales.AdContract", on_delete=models.PROTECT, related_name="invoices"
    )
    bill_to_agency = models.BooleanField()  # 既定 = agency あり
    subtotal = models.IntegerField()  # 税抜合計(円)。マイナス可 (調整のみの赤伝)
    commission_amount = models.IntegerField(default=0)  # floor(subtotal × rate / 100)
    tax_rate = models.DecimalField(max_digits=4, decimal_places=2, default=10.00)
    tax_amount = models.IntegerField()
    total = models.IntegerField()
    status = models.CharField(
        max_length=8, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT
    )
    issued_at = models.DateTimeField(null=True, blank=True)
    pdf_r2_key = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # 誰が発行し、誰が失効させたか (#sec L-4)。監査ログ (icstv.security) にも出しているが、
    # ログは Loki の retention (7 日) で消える。会計帳簿の保存期間 (10 年) を満たすには
    # DB 側に残す必要があるため両方持つ。SET_NULL なのは、退職等で User を消しても
    # 請求書そのものは残さなければならないため (証跡としては監査ログ側に username が残る)。
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "invoice"
        constraints = [
            models.CheckConstraint(
                name="chk_invoice_total",
                condition=Q(total=F("subtotal") - F("commission_amount") + F("tax_amount")),
            )
        ]

    def __str__(self):
        return self.invoice_number


class InvoiceLine(models.Model):
    """請求明細。調整行 (make_good 精算) は両 NULL 可。"""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.TextField()
    quantity = models.IntegerField()
    unit_price = models.IntegerField()  # マイナス可 (調整行)
    amount = models.IntegerField()
    spot_order = models.ForeignKey(
        "sales.SpotOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    sponsorship = models.ForeignKey(
        "sales.Sponsorship", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    make_good = models.ForeignKey(
        "sales.MakeGood",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_lines",
    )  # 調整行の精算元 (billing→sales 方向)

    class Meta:
        db_table = "invoice_line"
        constraints = [
            models.CheckConstraint(
                name="chk_line_one",
                condition=~Q(spot_order__isnull=False, sponsorship__isnull=False),
            )
        ]
        indexes = [
            models.Index(
                fields=["make_good"],
                condition=Q(make_good__isnull=False),
                name="idx_invoice_line_make_good",
            ),
        ]


class BroadcastCertificate(models.Model):
    """放送確認書 (発行時に明細を確定保存。program 削除に依らず不変)。"""

    period = models.ForeignKey(BillingPeriod, on_delete=models.PROTECT, related_name="certificates")
    contract = models.ForeignKey(
        "sales.AdContract", on_delete=models.PROTECT, related_name="certificates"
    )
    detail = models.JSONField(default=dict)  # 発行時点の airing 明細スナップショット
    pdf_r2_key = models.CharField(max_length=500, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "broadcast_certificate"


class Payment(models.Model):
    """入金 (手動登録)。"""

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    paid_amount = models.IntegerField()
    paid_at = models.DateField()
    method = models.CharField(max_length=100, blank=True, null=True)  # 振込等
    note = models.TextField(blank=True, null=True)
    # 誰が入金を登録したか (#sec L-4)。上の Invoice と同じ理由で DB にも残す。
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        db_table = "payment"
