# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ホスト別 urlconf 切替 (#7 公開/管理ドメイン分離) と Content-Security-Policy 付与 (#sec L-11)。

ホストを 3 区分で urlconf に割り当てる:
  - 管理ホスト (settings.ICSTV_ADMIN_HOSTS)        → ROOT_URLCONF (config.urls, フル機能)
  - 放送ホスト (settings.ICSTV_OPS_HOSTS)          → OPS_URLCONF (config.urls_ops, 🔴放送コンソールのみ・リファクタ Phase 1)
  - それ以外/未知                                   → PUBLIC_URLCONF (config.urls_public, 視聴者向けのみ)

fail-safe: 未知ホストは最も制限の強い公開側に倒す。
納品ポータルは別リポ ICS-DELIVERY サービスへ分離 (Phase 3.9)。旧 deliver.* ホスト用の
DELIVERY_URLCONF は撤去した。
"""

from __future__ import annotations

from django.conf import settings


class HostUrlconfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # settings は毎回参照 (テストの override_settings を効かせるため。コストは無視できる)
        admin_hosts = {h.lower() for h in getattr(settings, "ICSTV_ADMIN_HOSTS", [])}
        ops_hosts = {h.lower() for h in getattr(settings, "ICSTV_OPS_HOSTS", [])}
        creator_hosts = {h.lower() for h in getattr(settings, "ICSTV_CREATOR_HOSTS", [])}
        host = request.get_host().split(":")[0].lower()
        if host in admin_hosts:
            pass  # ROOT_URLCONF (フル機能)
        elif host in ops_hosts:
            # 🔴 放送コンソール専用ホスト (ops.*)。放送 SPA + API + 認証のみ (リファクタ Phase 1)。
            request.urlconf = getattr(settings, "OPS_URLCONF", "config.urls_ops")
        elif host in creator_hosts:
            # クリエイター専用ホスト (creator.*)。#27 ファンクラブ、Google招待サインイン限定。
            request.urlconf = getattr(settings, "CREATOR_URLCONF", "config.urls_creator")
        else:
            request.urlconf = getattr(settings, "PUBLIC_URLCONF", "config.urls_public")
        return self.get_response(request)


class ContentSecurityPolicyMiddleware:
    """CSP を 2 本立てで付与する (#sec L-11)。

    settings.CSP_ENFORCE が Content-Security-Policy、settings.CSP_REPORT_ONLY が
    Content-Security-Policy-Report-Only になる。空文字なら該当ヘッダを付けない。
    段階導入の意図と、いま enforce できない理由は settings 側のコメントを参照。

    既にヘッダが載っているレスポンスは上書きしない。個別のビューが自分の都合で
    (例えば埋め込み許可のために) 緩めた CSP を、後段のミドルウェアが黙って
    締め直すと原因の分からない不具合になるため。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        for header, setting_name in (
            ("Content-Security-Policy", "CSP_ENFORCE"),
            ("Content-Security-Policy-Report-Only", "CSP_REPORT_ONLY"),
        ):
            policy = getattr(settings, setting_name, "") or ""
            if policy and header not in response:
                response[header] = policy
        return response
