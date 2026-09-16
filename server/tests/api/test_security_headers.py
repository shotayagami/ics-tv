# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""§L-10/L-11: セキュリティヘッダと CSP のテスト。

観点は 2 つ。

1. **既定値に依存していた 3 ヘッダを settings で固定できているか。**
   点検 (2026-07-03) の時点でも nosniff / Referrer-Policy / X-Frame-Options は
   実際には出ていた。Django の既定値がそのまま効いていたためだが、settings に記述が無いと
   「未設定」と読み違えるうえ、Django のメジャーアップグレードで既定が変わっても気付けない。
2. **CSP が 2 本立てで付いているか。** enforce 側は壊しようがない指示子だけ、
   完全版は Report-Only。段階導入の意図は settings のコメントを参照。
"""

from __future__ import annotations

import pytest
from django.test import override_settings

_PUBLIC = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_OPS_HOSTS=[], ICSTV_CREATOR_HOSTS=[])


@pytest.fixture
def public_response(client):
    """公開ホストの任意のレスポンス。ヘッダはミドルウェアが全経路に付けるので中身は問わない。"""
    with _PUBLIC:
        return client.get("/")


@pytest.mark.django_db
def test_content_type_nosniff_is_set(public_response):
    assert public_response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
def test_referrer_policy_is_same_origin(public_response):
    assert public_response.headers["Referrer-Policy"] == "same-origin"


@pytest.mark.django_db
def test_x_frame_options_denies_framing(public_response):
    assert public_response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.django_db
def test_csp_enforce_has_only_the_safe_directives(public_response):
    """enforce 側に script-src / style-src を入れない。

    テンプレートは inline のイベントハンドラと inline <style> を持つため、
    ここへ script-src や style-src を足した瞬間に公開サイトが壊れる。
    第 2 段で inline を外すまでは、この 3 指示子だけに保つ。
    """
    csp = public_response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "object-src 'none'" in csp
    assert "script-src" not in csp
    assert "style-src" not in csp


@pytest.mark.django_db
def test_csp_report_only_carries_the_full_policy(public_response):
    """完全版は Report-Only 側にある (違反の実績を見てから enforce へ寄せる)。"""
    ro = public_response.headers["Content-Security-Policy-Report-Only"]
    assert "default-src 'self'" in ro
    assert "script-src" in ro
    assert "form-action 'self'" in ro


@pytest.mark.django_db
def test_hsts_is_not_emitted_by_django(public_response):
    """HSTS は TLS を終端する層 (Cloudflare / ingress-nginx) が付ける。

    Django 側でも出すと nginx の add_header と重複して 2 本になる。
    どちらが正か分からなくなるので一本化する、という判断を固定するテスト。
    """
    assert "Strict-Transport-Security" not in public_response.headers


@override_settings(CSP_ENFORCE="", CSP_REPORT_ONLY="")
@pytest.mark.django_db
def test_empty_policy_means_no_header(client):
    """空文字なら該当ヘッダを付けない (段階導入の途中で片方だけ落とせる)。"""
    with _PUBLIC:
        res = client.get("/")
    assert "Content-Security-Policy" not in res.headers
    assert "Content-Security-Policy-Report-Only" not in res.headers


@override_settings(CSP_ENFORCE="default-src 'none'")
def test_existing_header_is_not_overwritten():
    """ビューが自分で付けた CSP を後段が黙って締め直さない。

    埋め込み許可などでビューが意図的に緩めた CSP を上書きすると、
    原因の分からない表示不具合になる。ミドルウェア単体で確認する。
    """
    from django.http import HttpResponse

    from core.middleware import ContentSecurityPolicyMiddleware

    def view(_request):
        res = HttpResponse("ok")
        res["Content-Security-Policy"] = "default-src 'self' https://embed.example"
        return res

    res = ContentSecurityPolicyMiddleware(view)(None)
    assert res["Content-Security-Policy"] == "default-src 'self' https://embed.example"
