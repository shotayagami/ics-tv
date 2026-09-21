# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#sec L-4/L-5: 金額を確定する操作の符号検証と監査証跡。

点検 (2026-07-03) の指摘は 2 つ。

- **L-4**: `close_month` / `issue_invoice` / `void_invoice` / `register_payment` が
  操作した User を記録していない。`BillingPeriod.closed_by` と
  `BroadcastCertificate.created_by` はフィールドが在るのに常に未設定だった。
- **L-5**: `register_payment` が負数・ゼロの入金額を検証せず、許容差額での自動 PAID 化も
  区別なく行う。

監査ログは `icstv.security` へ出す (点検の結論 3 点目が「認証・**課金**・内部トークンの
security ログ」をひとまとめにしているため)。`test_security_log.py` と同じく
propagate=False なので、caplog のハンドラを当該ロガーへ直接付けて捕捉する。
"""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest
from django.contrib.auth import get_user_model

from billing import services
from billing.models import BillingPeriod, Invoice, InvoiceStatus, Payment
from billing.services import BillingError, issue_invoice, register_payment, void_invoice
from sales.models import AdContract, Advertiser, ContractKind, Industry


@pytest.fixture
def audit_logs(caplog):
    caplog.set_level(logging.INFO, logger="icstv.security")
    lg = logging.getLogger("icstv.security")
    lg.addHandler(caplog.handler)
    yield caplog
    lg.removeHandler(caplog.handler)


def _events(caplog, event: str) -> list[dict]:
    out = []
    for r in caplog.records:
        if r.name != "icstv.security":
            continue
        payload = json.loads(r.getMessage())
        if payload.get("event") == event:
            out.append(payload)
    return out


@pytest.fixture
def fake_r2(monkeypatch):
    class _Client:
        def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
            pass

    monkeypatch.setattr(services.r2, "client", lambda: _Client())
    monkeypatch.setattr(services.r2, "bucket", lambda: "b")


def _staff():
    return get_user_model().objects.create_user(
        username="keiri", password="x", is_staff=True
    )  # pragma: allowlist secret - test only


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


# --------------------------------------------------------------------------- #
#  L-5: 符号の検証                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [0, -1, -100000])
def test_register_payment_rejects_non_positive_amount(db, bad):
    """負数・ゼロの入金は弾く。

    **検証を services に置いたことの確認**でもある。入口は旧 op_pay と admin API の
    2 つあり、どちらも int にパースするだけだった。入口側に足すと、もう片方から素通りする。
    """
    inv = _invoice(status=InvoiceStatus.ISSUED)
    with pytest.raises(BillingError):
        register_payment(inv, bad, paid_at=date(2026, 7, 1))
    assert Payment.objects.count() == 0  # 行も作られない
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.ISSUED


def test_register_payment_accepts_positive_amount(db):
    inv = _invoice(status=InvoiceStatus.ISSUED)
    assert register_payment(inv, 110000, paid_at=date(2026, 7, 1)) == InvoiceStatus.PAID


# --------------------------------------------------------------------------- #
#  L-5: 満額でないのに PAID になる経路を区別して記録する                        #
# --------------------------------------------------------------------------- #


def test_tolerance_clearing_is_recorded_as_such(db, audit_logs):
    """許容差額での消込は cleared_by=tolerance と shortfall を残す。

    金額が合っていないのに PAID になる経路なので、後から抽出できないと検知できない。
    """
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    register_payment(inv, 109500, paid_at=date(2026, 7, 1))  # 500 円不足 (既定 tolerance 1000)
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.PAID
    ev = _events(audit_logs, "billing.register_payment")[-1]
    assert ev["cleared_by"] == "tolerance"
    assert ev["shortfall"] == 500


def test_overpayment_is_distinguished_from_exact(db, audit_logs):
    """過払いも無条件 PAID になるので exact と区別する (点検 L-5 の「別途検知」)。"""
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    register_payment(inv, 120000, paid_at=date(2026, 7, 1))
    ev = _events(audit_logs, "billing.register_payment")[-1]
    assert ev["cleared_by"] == "overpaid"
    assert ev["shortfall"] == -10000


def test_exact_payment_is_labelled_exact(db, audit_logs):
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    register_payment(inv, 110000, paid_at=date(2026, 7, 1))
    ev = _events(audit_logs, "billing.register_payment")[-1]
    assert ev["cleared_by"] == "exact"
    assert ev["shortfall"] == 0


# --------------------------------------------------------------------------- #
#  L-4: 操作者と前後値                                                          #
# --------------------------------------------------------------------------- #


def test_payment_audit_records_actor_and_before_after(db, audit_logs):
    user = _staff()
    inv = _invoice(status=InvoiceStatus.ISSUED, total=110000)
    register_payment(inv, 60000, paid_at=date(2026, 7, 1), actor=user)
    register_payment(inv, 50000, paid_at=date(2026, 7, 2), actor=user)
    evs = _events(audit_logs, "billing.register_payment")
    assert [e["actor"] for e in evs] == ["keiri", "keiri"]
    # 2 回目は「入金累計 60000 → 110000」が残る
    assert evs[1]["paid_total_before"] == 60000
    assert evs[1]["paid_total_after"] == 110000
    assert evs[1]["status_before"] == InvoiceStatus.ISSUED
    assert evs[1]["status_after"] == InvoiceStatus.PAID


def test_issue_and_void_record_actor_and_amount(db, fake_r2, audit_logs):
    user = _staff()
    inv = _invoice()
    issue_invoice(inv, actor=user)
    void_invoice(inv, actor=user)

    issued = _events(audit_logs, "billing.issue_invoice")[-1]
    assert issued["actor"] == "keiri"
    assert issued["status_before"] == InvoiceStatus.DRAFT
    assert issued["status_after"] == InvoiceStatus.ISSUED

    voided = _events(audit_logs, "billing.void_invoice")[-1]
    assert voided["actor"] == "keiri"
    # 「いくらの請求が消えたか」を残す
    assert voided["total"] == 110000
    assert voided["status_after"] == InvoiceStatus.VOID


def test_close_month_persists_closed_by(db):
    """フィールドは在ったのに常に未設定だった、が L-4 の指摘そのもの。"""
    user = _staff()
    services.close_month(2026, 6, force=True, actor=user)
    period = BillingPeriod.objects.get(year=2026, month=6)
    assert period.closed_at is not None
    assert period.closed_by == user


def test_actor_none_is_recorded_as_system(db, audit_logs):
    """人手を介さない経路は actor="system"。

    「記録し忘れ」と「そもそも人がいない」を後から区別できるようにするため、
    None を黙って落とさない。
    """
    inv = _invoice(status=InvoiceStatus.ISSUED)
    register_payment(inv, 110000, paid_at=date(2026, 7, 1))
    ev = _events(audit_logs, "billing.register_payment")[-1]
    assert ev["actor"] == "system"
    assert "actor_id" not in ev  # None のフィールドは emit が落とす


# --------------------------------------------------------------------------- #
#  L-4: DB への永続化 (会計帳簿の保存期間 10 年に対応)                          #
#                                                                              #
#  監査ログ (icstv.security) はログ基盤の retention で消え長期証跡にならない。      #
#  「誰が確定させたか」は各モデルの *_by / *_at 列で持つ。                        #
# --------------------------------------------------------------------------- #


def test_issue_persists_issued_by(db, fake_r2):
    user = _staff()
    inv = _invoice()
    issue_invoice(inv, actor=user)
    inv.refresh_from_db()
    assert inv.issued_by == user
    assert inv.issued_at is not None


def test_void_persists_voided_by_and_at(db, fake_r2):
    user = _staff()
    inv = _invoice(status=InvoiceStatus.ISSUED)
    void_invoice(inv, actor=user)
    inv.refresh_from_db()
    assert inv.voided_by == user
    assert inv.voided_at is not None
    # 発行と失効は別の列なので、両方の操作者が同時に残る
    assert inv.issued_by is None  # このテストでは発行を経ていない


def test_payment_persists_registered_by(db):
    user = _staff()
    inv = _invoice(status=InvoiceStatus.ISSUED)
    register_payment(inv, 110000, paid_at=date(2026, 7, 1), actor=user)
    pay = Payment.objects.get(invoice=inv)
    assert pay.registered_by == user


def test_operator_survives_user_deletion(db):
    """User を消しても請求書と入金の行は残る (SET_NULL)。

    退職等で User を消したときに請求書ごと消えては困る。操作者の氏名そのものは
    監査ログ側に username として残る。
    """
    user = _staff()
    inv = _invoice(status=InvoiceStatus.ISSUED)
    register_payment(inv, 110000, paid_at=date(2026, 7, 1), actor=user)
    user.delete()
    inv.refresh_from_db()
    pay = Payment.objects.get(invoice=inv)
    assert pay.registered_by is None
    assert pay.paid_amount == 110000  # 金額は残る
    assert inv.status == InvoiceStatus.PAID
