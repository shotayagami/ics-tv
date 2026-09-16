# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""運営者表示 (D010): tokushoho/privacy/terms が settings.ICSTV_OPERATOR_* を表示すること。

架空の値のみを使う (計画 §7 P2 完了条件)。実際の事業者名・住所・電話・メールは
settings 側 (env 未設定時は空文字) にのみ存在し、テンプレート・テストのどちらにも書かない。
"""

from __future__ import annotations

from django.test import override_settings

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])

_FAKE_OPERATOR = {
    "ICSTV_OPERATOR_LEGAL_NAME": "架空商事株式会社",
    "ICSTV_OPERATOR_REPRESENTATIVE_NAME": "架空 太郎",
    "ICSTV_OPERATOR_ADDRESS": "架空県架空市架空町1-2-3",
    "ICSTV_OPERATOR_PHONE": "000-0000-0000",
    "ICSTV_OPERATOR_HIDE_CONTACT_DETAILS": False,
    "ICSTV_OPERATOR_CONTACT_EMAIL": "legal@example.invalid",
}


def _apply_fake_operator(settings) -> None:
    for key, value in _FAKE_OPERATOR.items():
        setattr(settings, key, value)


@_PUBLIC_HOST
def test_tokushoho_shows_operator_identification(http_client, db, settings):
    _apply_fake_operator(settings)
    body = http_client.get("/tokushoho/").content.decode("utf-8")
    assert "架空商事株式会社" in body
    assert "架空 太郎" in body
    assert "架空県架空市架空町1-2-3" in body
    assert "000-0000-0000" in body
    assert 'mailto:legal@example.invalid"' in body


@_PUBLIC_HOST
def test_tokushoho_hide_contact_details_true_omits_address_and_shows_disclosure_notice(
    http_client, db, settings
):
    _apply_fake_operator(settings)
    settings.ICSTV_OPERATOR_HIDE_CONTACT_DETAILS = True
    body = http_client.get("/tokushoho/").content.decode("utf-8")
    assert "請求があった場合は遅滞なく開示します" in body
    assert "架空県架空市架空町1-2-3" not in body
    assert "000-0000-0000" not in body


@_PUBLIC_HOST
def test_tokushoho_unset_operator_renders_placeholder(http_client, db):
    """未設定 (既定の空文字/True) でも起動・描画でき、空欄はプレースホルダになること。

    title タグ側にも "—" (区切り) が出るため、「事業者名」「運営統括責任者」の
    <p>—</p> 単体で判定する (title の "—" と誤って一致させないため)。
    """
    res = http_client.get("/tokushoho/")
    assert res.status_code == 200
    body = res.content.decode("utf-8")
    assert body.count("<p>—</p>") == 2


@_PUBLIC_HOST
def test_privacy_shows_operator_contact_email(http_client, db, settings):
    _apply_fake_operator(settings)
    body = http_client.get("/privacy/").content.decode("utf-8")
    assert 'mailto:legal@example.invalid"' in body


@_PUBLIC_HOST
def test_terms_shows_operator_contact_email(http_client, db, settings):
    _apply_fake_operator(settings)
    body = http_client.get("/terms/").content.decode("utf-8")
    assert 'mailto:legal@example.invalid"' in body
