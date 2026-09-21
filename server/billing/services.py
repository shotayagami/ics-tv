# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""月次締め close_month (#6 / docs/sales.md 請求精算フロー)。

airing (放確台帳) を締めてロックし、契約ごとに請求書 (invoice) + 放送確認書 (broadcast_certificate)
を生成する。金額は本数×単価 (S9)。手数料 floor、税額 floor (税率ごと1回)、total は CHECK で強制。
billed=true / issued 済み invoice は不変。再発行は void→新規 (本サービスでは扱わない)。
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from billing.models import (
    BillingPeriod,
    BroadcastCertificate,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
)
from core import r2
from core.security_log import emit_audit as _audit
from playout.models import PlayoutAction, PlayoutEvent, PlayoutStatus
from sales.models import Airing, MakeGood, MakeGoodStatus, Sponsorship

logger = logging.getLogger(__name__)


class BillingError(Exception):
    pass


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _end_dt(end: date):
    return timezone.make_aware(datetime.combine(end, time.max))


def _start_dt(start: date):
    return timezone.make_aware(datetime.combine(start, time.min))


def precheck(year: int, month: int) -> list[str]:
    """締め前チェック (警告のみ)。as-run 未返送 / sponsorship 期間矛盾。"""
    warnings: list[str] = []
    start, end = _month_bounds(year, month)
    unsent = PlayoutEvent.objects.filter(
        action__in=(PlayoutAction.PLAY_CM, PlayoutAction.PLAY_CM_BUNDLE),
        status__in=(PlayoutStatus.SCHEDULED, PlayoutStatus.EXECUTING),
        scheduled_at__gte=_start_dt(start),
        scheduled_at__lte=_end_dt(end),
    ).count()
    if unsent:
        warnings.append(
            f"当月に as-run 未返送の CM が {unsent} 件あります (store-and-forward 遅延)"
        )
    return warnings


def _commission(subtotal: int, bill_to_agency: bool, rate: Decimal) -> int:
    if not bill_to_agency or rate <= 0:
        return 0
    return int(subtotal * rate / 100)  # floor (int 切捨て)


def _tax(taxable: int, tax_rate: Decimal) -> int:
    return int(taxable * tax_rate / 100)  # floor、税率ごとに1回


def _spot_lines(contract, airings, month_start) -> list[dict]:
    """spot_order ごとに 本数×単価。過月分 (aired_at < 当月) は別明細に分ける。"""
    cur: dict[int, list] = defaultdict(list)
    past: dict[int, list] = defaultdict(list)
    for a in airings:
        if a.spot_order_id is None:
            continue
        (cur if a.aired_at.date() >= month_start else past)[a.spot_order_id].append(a)
    lines = []
    for bucket, label in ((cur, "当月分"), (past, "前月分")):
        for so_id, rows in bucket.items():
            so = rows[0].spot_order
            qty = len(rows)
            lines.append(
                {
                    "description": f"スポットCM {label} {qty}本",
                    "quantity": qty,
                    "unit_price": so.unit_price,
                    "amount": qty * so.unit_price,
                    "spot_order_id": so_id,
                }
            )
    return lines


def _sponsorship_lines(contract, year, month, start, end) -> list[dict]:
    """タイム契約: monthly_fee × (月内有効日数 / 当月暦日数) で日割り。"""
    days_in_month = end.day
    lines = []
    for sp in Sponsorship.objects.filter(contract=contract):
        ps = sp.period_start or contract.period_start
        pe = sp.period_end or contract.period_end
        lo, hi = max(ps, start), min(pe, end)
        if lo > hi:
            continue  # 当月に有効期間なし
        valid_days = (hi - lo).days + 1
        amount = sp.monthly_fee * valid_days // days_in_month
        lines.append(
            {
                "description": f"番組提供料 {year}-{month:02d}分 ({valid_days}/{days_in_month}日)",
                "quantity": 1,
                "unit_price": amount,
                "amount": amount,
                "sponsorship_id": sp.id,
            }
        )
    return lines


def _credited_make_good_lines(contract) -> list[dict]:
    """未請求の credited make_good を減額 (調整行)。有効 invoice_line から未参照のもの。"""
    referenced = set(
        InvoiceLine.objects.filter(
            make_good__isnull=False,
            invoice__status__in=(InvoiceStatus.DRAFT, InvoiceStatus.ISSUED, InvoiceStatus.PAID),
        ).values_list("make_good_id", flat=True)
    )
    lines = []
    mgs = MakeGood.objects.filter(
        status=MakeGoodStatus.CREDITED, spot_order__contract=contract
    ).select_related("spot_order")
    for mg in mgs:
        if mg.id in referenced:
            continue
        amount = -(mg.spot_order.unit_price if mg.spot_order is not None else 0)
        lines.append(
            {
                "description": "前月欠送分減額",
                "quantity": 1,
                "unit_price": amount,
                "amount": amount,
                "spot_order_id": mg.spot_order_id,
                "make_good_id": mg.id,
            }
        )
    return lines


def _next_seq(period: BillingPeriod) -> int:
    return Invoice.objects.filter(period=period).count() + 1


def _build_invoice(
    period, contract, airings, year, month, start, end, seq, actor=None
) -> Invoice | None:
    line_specs = (
        _spot_lines(contract, airings, start)
        + _sponsorship_lines(contract, year, month, start, end)
        + _credited_make_good_lines(contract)
    )
    if not line_specs:
        return None
    subtotal = sum(s["amount"] for s in line_specs)
    bill_to_agency = contract.agency_id is not None
    rate = contract.agency.commission_rate if (bill_to_agency and contract.agency) else Decimal(0)
    commission = _commission(subtotal, bill_to_agency, rate)
    tax_rate = Decimal("10.00")
    tax = _tax(subtotal - commission, tax_rate)
    invoice = Invoice.objects.create(
        invoice_number=f"INV-{year}-{month:02d}-{seq:04d}",
        period=period,
        contract=contract,
        bill_to_agency=bill_to_agency,
        subtotal=subtotal,
        commission_amount=commission,
        tax_rate=tax_rate,
        tax_amount=tax,
        total=subtotal - commission + tax,
    )
    InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(
                invoice=invoice,
                description=s["description"],
                quantity=s["quantity"],
                unit_price=s["unit_price"],
                amount=s["amount"],
                spot_order_id=s.get("spot_order_id"),
                sponsorship_id=s.get("sponsorship_id"),
                make_good_id=s.get("make_good_id"),
            )
            for s in line_specs
        ]
    )
    # 放送確認書 (airing スナップショット)
    BroadcastCertificate.objects.create(
        period=period,
        contract=contract,
        created_by=actor,  # #sec L-4: フィールドは在ったが常に未設定だった
        detail={
            "airings": [
                {
                    "aired_at": a.aired_at.isoformat(),
                    "program_title": a.program_title,
                    "cm_asset_id": a.cm_asset_id,
                    "duration_ms": a.duration_ms,
                }
                for a in airings
            ]
        },
    )
    return invoice


def close_month(year: int, month: int, *, force: bool = False, actor=None) -> dict:
    """月次締め。警告ありで force=False なら締めず警告のみ返す。"""
    warnings = precheck(year, month)
    if warnings and not force:
        return {"closed": False, "warnings": warnings}

    start, end = _month_bounds(year, month)
    end_dt = _end_dt(end)
    with transaction.atomic():
        period, _ = BillingPeriod.objects.select_for_update().get_or_create(year=year, month=month)
        if period.closed_at is not None:
            raise BillingError(f"{year}-{month:02d} は既に締め済です")

        # 締め対象 = billed=false AND aired_at <= 当月末 (過月の遅延出現も回収)
        scope = list(
            # of=("self",): nullable FK (spot_order/sponsorship) の outer join 側はロックしない
            # (FOR UPDATE cannot be applied to the nullable side of an outer join を回避)
            Airing.objects.select_for_update(of=("self",))
            .filter(billed=False, aired_at__lte=end_dt)
            .select_related("spot_order__contract", "sponsorship__contract")
        )
        by_contract: dict[int, list] = defaultdict(list)
        contract_map = {}
        for a in scope:
            c = None
            if a.spot_order is not None:
                c = a.spot_order.contract
            elif a.sponsorship is not None:
                c = a.sponsorship.contract
            if c is not None:
                by_contract[c.id].append(a)
                contract_map[c.id] = c
        Airing.objects.filter(id__in=[a.id for a in scope]).update(billed=True)

        # 当月に有効な sponsorship を持つ契約 / credited make_good 契約も対象に含める
        from sales.models import AdContract

        for sp in Sponsorship.objects.filter(
            contract__period_start__lte=end, contract__period_end__gte=start
        ).select_related("contract"):
            contract_map.setdefault(sp.contract_id, sp.contract)
        for mg in MakeGood.objects.filter(
            status=MakeGoodStatus.CREDITED, spot_order__isnull=False
        ).select_related("spot_order__contract"):
            if mg.spot_order is not None:
                contract_map.setdefault(mg.spot_order.contract_id, mg.spot_order.contract)
        # 念のため未取得 contract を埋める
        missing = set(by_contract) - set(contract_map)
        for c in AdContract.objects.filter(id__in=missing):
            contract_map[c.id] = c

        invoices = []
        seq = _next_seq(period)
        for cid in sorted(contract_map):
            inv = _build_invoice(
                period,
                contract_map[cid],
                by_contract.get(cid, []),
                year,
                month,
                start,
                end,
                seq,
                actor,
            )
            if inv:
                invoices.append(inv)
                seq += 1

        period.closed_at = timezone.now()
        period.closed_by = actor  # #sec L-4: 誰が締めたかを残す (内部統制/non-repudiation)
        period.save(update_fields=["closed_at", "closed_by"])
    _audit(
        "billing.close_month",
        actor=actor,
        period=f"{year}-{month:02d}",
        period_id=period.id,
        invoice_count=len(invoices),
        invoice_total=sum(i.total for i in invoices),
        forced=force,
        warning_count=len(warnings),
    )

    return {
        "closed": True,
        "period_id": period.id,
        "invoice_ids": [i.id for i in invoices],
        "warnings": warnings,
    }


# ---- 発行 / 失効 / 入金 (B3) ----


def _render_and_store_pdf(invoice: Invoice) -> str:
    """請求書 HTML を描画 → WeasyPrint で PDF 化 (未導入なら HTML フォールバック) → R2 保存。"""
    from django.template.loader import render_to_string

    html = render_to_string(
        "billing/invoice_pdf.html",
        {
            "invoice": invoice,
            "lines": list(invoice.lines.all()),
            "reg_no": getattr(settings, "ICSTV_INVOICE_REG_NO", ""),
        },
    )
    try:
        import weasyprint

        body = weasyprint.HTML(string=html).write_pdf()
        ctype = "application/pdf"
    except Exception:
        logger.warning("WeasyPrint 不在/失敗 → HTML で保存 (invoice=%s)", invoice.invoice_number)
        body = html.encode("utf-8")
        ctype = "text/html"
    key = f"billing/invoice/{invoice.invoice_number}.pdf"
    r2.client().put_object(Bucket=r2.bucket(), Key=key, Body=body, ContentType=ctype)
    return key


def issue_invoice(invoice: Invoice, *, actor=None) -> Invoice:
    """draft → issued。PDF を生成し R2 に保存。"""
    if invoice.status != InvoiceStatus.DRAFT:
        raise BillingError("draft の invoice のみ発行できます")
    before = invoice.status
    key = _render_and_store_pdf(invoice)
    invoice.status = InvoiceStatus.ISSUED
    invoice.issued_at = timezone.now()
    invoice.issued_by = actor
    invoice.pdf_r2_key = key
    invoice.save(update_fields=["status", "issued_at", "issued_by", "pdf_r2_key"])
    _audit(
        "billing.issue_invoice",
        actor=actor,
        invoice_id=invoice.pk,
        invoice_number=invoice.invoice_number,
        total=invoice.total,
        status_before=before,
        status_after=invoice.status,
    )
    return invoice


def void_invoice(invoice: Invoice, *, actor=None) -> Invoice:
    """issued/paid → void (再発行は新規 invoice を別途生成)。"""
    if invoice.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PAID):
        raise BillingError("issued/paid の invoice のみ void できます")
    before = invoice.status
    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = timezone.now()
    invoice.voided_by = actor
    invoice.save(update_fields=["status", "voided_at", "voided_by"])
    # 失効は「確定していた金額を取り消す」操作なので、金額も一緒に残す
    # (後から「いくらの請求が誰の判断で消えたか」を追えるようにするため)。
    _audit(
        "billing.void_invoice",
        actor=actor,
        invoice_id=invoice.pk,
        invoice_number=invoice.invoice_number,
        total=invoice.total,
        status_before=before,
        status_after=invoice.status,
    )
    return invoice


def register_payment(
    invoice: Invoice, paid_amount: int, paid_at, method: str = "", note: str = "", *, actor=None
) -> str:
    """入金登録 → 消込。全額一致 or 許容差額以内の不足で paid。

    #sec L-5: 金額はここで符号を検証する。呼び出し側 (旧 op_pay / admin API) は
    どちらも int にパースするだけで負数・ゼロを弾いていなかった。**検証は入口ではなく
    ここに置く** — 入口が 2 つある以上、片方に足しても もう片方から素通りするため。
    """
    if invoice.status == InvoiceStatus.VOID:
        raise BillingError("void の invoice には入金できません")
    if paid_amount <= 0:
        raise BillingError("入金額は 1 円以上で指定してください")
    tolerance = getattr(settings, "ICSTV_PAYMENT_TOLERANCE", 1000)
    status_before = invoice.status
    with transaction.atomic():
        paid_before = invoice.payments.aggregate(s=Sum("paid_amount"))["s"] or 0
        Payment.objects.create(
            invoice=invoice,
            paid_amount=paid_amount,
            paid_at=paid_at,
            method=method,
            note=note,
            registered_by=actor,
        )
        total_paid = invoice.payments.aggregate(s=Sum("paid_amount"))["s"] or 0
        shortfall = invoice.total - total_paid
        cleared_by = ""
        if shortfall <= 0:
            invoice.status = InvoiceStatus.PAID
            invoice.save(update_fields=["status"])
            # shortfall < 0 は過払い。金額が合っていないのに PAID になるので区別して残す。
            cleared_by = "overpaid" if shortfall < 0 else "exact"
        elif shortfall <= tolerance:
            invoice.status = InvoiceStatus.PAID
            invoice.save(update_fields=["status"])
            cleared_by = "tolerance"
            Payment.objects.filter(invoice=invoice).order_by("-id").update(
                note=f"{note} (差額 {shortfall}円を許容差額で消込)".strip()
            )
    # 許容差額・過払いでの消込は「満額でないのに PAID」なので、監査側で拾えるよう
    # cleared_by と shortfall を必ず残す (点検 L-5 の「別途検知」に対応)。
    _audit(
        "billing.register_payment",
        actor=actor,
        invoice_id=invoice.pk,
        invoice_number=invoice.invoice_number,
        invoice_total=invoice.total,
        paid_amount=paid_amount,
        paid_total_before=paid_before,
        paid_total_after=total_paid,
        shortfall=shortfall,
        cleared_by=cleared_by,
        status_before=status_before,
        status_after=invoice.status,
    )
    return invoice.status


def period_summaries(periods) -> list[dict]:
    """期ごとに請求額・入金額・未収を添えた行を、渡された順で返す。

    数え方は読み取り seam (`/internal/backoffice/billing`) と同じにする。void の請求書は
    請求額からも、その請求書に付いた入金からも落とす (void は「確定していた金額の取り消し」
    なので、受け取った現金も帳簿面から同時に消える)。draft の請求書は請求額に含む。

    **invoice と payment を 1 本の queryset で同時に集約してはいけない。** 期→invoice→payment の
    LEFT JOIN で invoice 行が入金件数ぶん複製され、請求額だけが水増しされる (入金側は正しい値の
    ままなので気付きにくい)。ここではクエリを 2 本に分けて Python で突き合わせる。期の数に
    依らず 2 クエリで済む。
    """
    ids = [p.pk for p in periods]
    invoiced = dict(
        Invoice.objects.filter(period_id__in=ids)
        .exclude(status=InvoiceStatus.VOID)
        .values("period_id")
        .annotate(s=Sum("total"))
        .values_list("period_id", "s")
    )
    paid = dict(
        Payment.objects.filter(invoice__period_id__in=ids)
        .exclude(invoice__status=InvoiceStatus.VOID)
        .values("invoice__period_id")
        .annotate(s=Sum("paid_amount"))
        .values_list("invoice__period_id", "s")
    )
    rows = []
    for period in periods:
        billed = invoiced.get(period.pk) or 0
        received = paid.get(period.pk) or 0
        rows.append(
            {
                "period": period,
                "invoiced": billed,
                "paid": received,
                "outstanding": billed - received,
            }
        )
    return rows
