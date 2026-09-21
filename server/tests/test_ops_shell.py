# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""🔴 放送コンソール (リファクタ Phase 1) のシェル配信 + ops.* ホスト振り分け。

ops.* は OPS_URLCONF (config.urls_ops) で動き、放送コンソール SPA + API + 認証のみを公開する。
管理 UI (編成/納品/経理 の Django 画面) は urls_ops に存在しないため、放送シェルが catch-all で
受ける (= studio.* のフル機能は出ない = 機能的に到達不能)。
"""

from __future__ import annotations

from django.test import Client, override_settings

OPS_HOST = "ops.testserver"
_OVERRIDE = override_settings(
    ICSTV_OPS_HOSTS=[OPS_HOST],
    ALLOWED_HOSTS=[OPS_HOST, "testserver"],
)


@_OVERRIDE
def test_ops_host_requires_staff(db):
    # 匿名は staff_member_required で login へ redirect (302)。
    res = Client().get("/", HTTP_HOST=OPS_HOST)
    assert res.status_code == 302


@_OVERRIDE
def test_ops_host_staff_mounts_spa(staff_client, db):
    body = staff_client.get("/", HTTP_HOST=OPS_HOST).content.decode("utf-8")
    assert 'id="ops-root"' in body
    assert "/static/web/ops/ops.js" in body
    assert "/static/web/ops/ops.css" in body


@_OVERRIDE
def test_ops_host_deeplink_serves_shell(staff_client, db):
    # /<slug> 等のディープリンクも同じシェルへ (React Router がクライアント側で解決)。
    body = staff_client.get("/ch1", HTTP_HOST=OPS_HOST).content.decode("utf-8")
    assert 'id="ops-root"' in body


@_OVERRIDE
def test_ops_host_hides_full_management(staff_client, db):
    # 編成 (full urlconf 専用) は ops.* に存在しない → catch-all で放送シェルが返る
    # (studio の編成 SPA は出ない)。
    body = staff_client.get("/scheduling/", HTTP_HOST=OPS_HOST).content.decode("utf-8")
    assert 'id="ops-root"' in body
    assert 'id="studio-root"' not in body


@_OVERRIDE
def test_ops_shell_no_comment_leak(staff_client, db):
    # Django {# #} は単一行専用。シェルがテンプレコメントを漏らさない。
    body = staff_client.get("/", HTTP_HOST=OPS_HOST).content.decode("utf-8")
    assert "{#" not in body


def test_admin_host_is_not_ops_shell(staff_client, db):
    # 管理ホスト (testserver=admin) の / は放送シェルではない (ホスト分離の確認)。
    body = staff_client.get("/").content.decode("utf-8")
    assert 'id="ops-root"' not in body


@_OVERRIDE
def test_ops_host_operations_wired(staff_client, db):
    # 送出操作 (core.urls.OPS_OPERATIONS) が放送ホストで解決する (catch-all シェルに飲まれない)。
    # op_clear_slate は @require_POST なので GET は 405 = ルート存在 + 操作ビューに到達 (P1.2)。
    res = staff_client.get("/ops/ch/ch1/clear-slate/", HTTP_HOST=OPS_HOST)
    assert res.status_code == 405
    assert b"ops-root" not in res.content
    # 押え/巻き (extend/shorten) も放送ホストで解決 (P1.3)。
    res2 = staff_client.get("/ops/ch/ch1/program/1/extend/", HTTP_HOST=OPS_HOST)
    assert res2.status_code == 405


def test_admin_host_operations_still_wired(staff_client, db):
    # OPS_OPERATIONS 切り出し後も studio.* (admin host) で送出操作が解決する (回帰)。
    res = staff_client.get("/ops/ch/ch1/clear-slate/")  # testserver = admin host
    assert res.status_code == 405
