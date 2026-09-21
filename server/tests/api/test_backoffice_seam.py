# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""バックオフィスサブシステム read seam (リファクタ Phase 3.1): /internal/backoffice/*。

ICS-BACKOFFICE (別リポ icstv-backoffice) が経理/予算/会員の集計を読み取る内部 API。
X-Internal-Token (BACKOFFICE_READ_TOKEN) 認証・読み取り専用。集計ロジックは既存 service を再利用。
"""

from __future__ import annotations

from django.test import Client, override_settings

TOK = "bo-tok"  # pragma: allowlist secret - test only
_OVR = override_settings(BACKOFFICE_READ_TOKEN=TOK)


def _get(path: str, token: str = TOK):
    return Client().get(path, headers={"X-Internal-Token": token} if token else {})


@_OVR
def test_requires_token(db):
    assert _get("/api/v1/internal/backoffice/member-stats", token="").status_code == 401
    assert _get("/api/v1/internal/backoffice/member-stats", token="wrong").status_code == 401


@_OVR
def test_token_unset_is_401(db):
    # トークン未設定 (空) なら常に 401 (= 集計取得は無効)。
    with override_settings(BACKOFFICE_READ_TOKEN=""):
        assert _get("/api/v1/internal/backoffice/member-stats").status_code == 401


@_OVR
def test_billing_empty_period_zeros(db):
    r = _get("/api/v1/internal/backoffice/billing?period=2099-01")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "period": "2099-01",
        "invoiced_total": 0,
        "paid_total": 0,
        "outstanding_total": 0,
        "invoices": [],
    }


@_OVR
def test_billing_bad_period_400(db):
    assert _get("/api/v1/internal/backoffice/billing?period=2026-13").status_code == 400
    assert _get("/api/v1/internal/backoffice/billing?period=nope").status_code == 400


@_OVR
def test_budget_is_empty(db):
    # 番組予算 (procurement) は追加提供側へ移したので、口と応答の形は残るが行は常に空 (D036/D037)。
    # 年度の引数はそのまま返る (呼び手が期間を区別できることの確認)。
    r = _get("/api/v1/internal/backoffice/budget?fiscal_year=2026")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"fiscal_year", "rows"}
    assert body["fiscal_year"] == 2026
    assert body["rows"] == []
    assert _get("/api/v1/internal/backoffice/budget?fiscal_year=2025").json()["fiscal_year"] == 2025


@_OVR
def test_member_stats_empty(db):
    body = _get("/api/v1/internal/backoffice/member-stats").json()
    assert body["total"] == 0
    assert body["active_30d"] == 0
    assert body["retention_rate"] is None
    assert {"plan": "無課金", "count": 0} in body["by_plan"]


@_OVR
def test_picker_token_required(db):
    assert _get("/api/v1/internal/backoffice/picker", token="").status_code == 401


@_OVR
def test_picker_lists_series_only(db):
    # series は本体の scheduling.Series なのでそのまま出る。納品一覧は番組予算 (procurement) と
    # ICS-DELIVERY 読み seam に依存し、どちらも追加提供側へ移したので常に空になる (D036/D037)。
    from core.models import Channel
    from scheduling.models import Series

    ch = Channel.objects.create(name="ch1", slug="ch1", enabled=True)
    s = Series.objects.create(channel=ch, title="連ドラ")

    body = _get("/api/v1/internal/backoffice/picker").json()
    assert set(body) == {"series", "deliveries"}
    assert {"id": s.id, "title": "連ドラ"} in body["series"]
    assert body["deliveries"] == []
