# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員管理 commit① : 登録/ログイン/ログアウト/プロフィール/デコレータ。

会員ルートは公開 urlconf (config.urls_public) 専用。テスト client 既定ホスト "testserver" は
ICSTV_ADMIN_HOSTS に含まれ管理 urlconf になるため、override_settings で公開 urlconf へ倒す。
"""

from __future__ import annotations

from django.test import RequestFactory, override_settings
from django.utils import timezone

from members.auth import SESSION_KEY
from members.models import Member

_PUBLIC_HOST = override_settings(ICSTV_ADMIN_HOSTS=[], ICSTV_DELIVERY_HOSTS=[])

_PW = "Tv9!kd83mfar"  # pragma: allowlist secret - test only
_WRONG = "nope-wrong-1!"  # pragma: allowlist secret - test only


def _reg_data(**over):
    data = {
        "email": "viewer@example.com",
        "password": _PW,
        "password_confirm": _PW,
        "nickname": "みんと",
        "birth_year": 1990,
        "birth_month": 4,
        "gender": "no_answer",
        "country": "JP",
        "postal_code": "100-0001",
        "agree": "on",
    }
    data.update(over)
    return data


def _make_member(email="m@example.com", **over):
    m = Member(
        email=email,
        nickname="tester",
        birth_year=1988,
        birth_month=6,
        gender="male",
        postal_code="1500001",
        **over,
    )
    m.set_password(_PW)
    m.save()
    return m


@_PUBLIC_HOST
def test_register_creates_member_and_logs_in(http_client, db):
    res = http_client.post("/members/register/", _reg_data())
    # 登録後はメール確認ページへ (commit②)。会員は作成済み・ログイン済み・未認証。
    assert res.status_code == 302 and res.url == "/members/verify/email/"
    m = Member.objects.get(email="viewer@example.com")
    assert m.nickname == "みんと"
    assert m.postal_code == "1000001"  # ハイフン正規化
    assert m.password and m.password != _PW  # ハッシュ保管 (平文でない)
    assert m.check_password(_PW)
    assert not m.is_verified  # 登録直後は未認証
    assert http_client.session[SESSION_KEY] == m.pk


@_PUBLIC_HOST
def test_register_password_mismatch(http_client, db):
    res = http_client.post("/members/register/", _reg_data(password_confirm="different1!"))
    assert res.status_code == 200
    assert not Member.objects.exists()


@_PUBLIC_HOST
def test_register_weak_password_rejected(http_client, db):
    res = http_client.post(
        "/members/register/", _reg_data(password="12345678", password_confirm="12345678")
    )
    assert res.status_code == 200
    assert not Member.objects.exists()


@_PUBLIC_HOST
def test_register_duplicate_email_rejected(http_client, db):
    _make_member(email="viewer@example.com")
    res = http_client.post("/members/register/", _reg_data(email="VIEWER@example.com"))
    assert res.status_code == 200  # 大文字小文字を無視して重複
    assert Member.objects.filter(email__iexact="viewer@example.com").count() == 1


@_PUBLIC_HOST
def test_register_bad_postal_rejected(http_client, db):
    res = http_client.post("/members/register/", _reg_data(postal_code="12ab"))
    assert res.status_code == 200
    assert not Member.objects.exists()


@_PUBLIC_HOST
def test_login_success_and_session_fixation(http_client, db):
    m = _make_member()
    s = http_client.session
    s["seed"] = "x"
    s.save()
    old_key = s.session_key
    res = http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    assert res.status_code == 302 and res.url == "/members/"
    assert http_client.session[SESSION_KEY] == m.pk
    assert http_client.session.session_key != old_key  # cycle_key でセッション固定化対策


@_PUBLIC_HOST
def test_login_wrong_password(http_client, db):
    _make_member()
    res = http_client.post("/members/login/", {"email": "m@example.com", "password": _WRONG})
    assert res.status_code == 200
    assert SESSION_KEY not in http_client.session


@_PUBLIC_HOST
def test_login_case_insensitive_email(http_client, db):
    m = _make_member(email="Case@Example.com")
    res = http_client.post("/members/login/", {"email": "case@example.com", "password": _PW})
    assert res.status_code == 302
    assert http_client.session[SESSION_KEY] == m.pk


@_PUBLIC_HOST
def test_login_next_rejects_backslash_open_redirect(http_client, db):
    """M-2: /\\evil.com はブラウザが //evil.com に正規化する。外部へ飛ばさず既定へ。"""
    _make_member()
    res = http_client.post(
        "/members/login/",
        {"email": "m@example.com", "password": _PW, "next": "/\\evil.com"},
    )
    assert res.status_code == 302 and res.url == "/members/"


@_PUBLIC_HOST
def test_login_next_rejects_protocol_relative(http_client, db):
    _make_member()
    res = http_client.post(
        "/members/login/",
        {"email": "m@example.com", "password": _PW, "next": "//evil.com"},
    )
    assert res.status_code == 302 and res.url == "/members/"


@_PUBLIC_HOST
def test_login_next_allows_safe_local_path(http_client, db):
    _make_member()
    res = http_client.post(
        "/members/login/",
        {"email": "m@example.com", "password": _PW, "next": "/members/history/"},
    )
    assert res.status_code == 302 and res.url == "/members/history/"


@_PUBLIC_HOST
def test_logout_clears_session(http_client, db):
    _make_member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    res = http_client.post("/members/logout/")
    assert res.status_code == 302
    assert SESSION_KEY not in http_client.session


@_PUBLIC_HOST
def test_profile_requires_login(http_client, db):
    res = http_client.get("/members/")
    assert res.status_code == 302
    assert res.url.startswith("/members/login/?next=")


@_PUBLIC_HOST
def test_profile_edit_updates_fields(http_client, db):
    m = _make_member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    res = http_client.post(
        "/members/edit/",
        {
            "nickname": "newname",
            "birth_year": 1995,
            "birth_month": 12,
            "gender": "female",
            "country": "JP",
            "postal_code": "5300001",
        },
    )
    assert res.status_code == 302
    m.refresh_from_db()
    assert m.nickname == "newname" and m.gender == "female" and m.postal_code == "5300001"


@_PUBLIC_HOST
def test_password_change_requires_current(http_client, db):
    m = _make_member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    new = "Zz8@mqp42hdk"  # pragma: allowlist secret - test only
    # 現在パスワード誤り → 失敗
    bad = http_client.post(
        "/members/password/",
        {"current_password": _WRONG, "new_password": new, "new_password_confirm": new},
    )
    assert bad.status_code == 200
    m.refresh_from_db()
    assert not m.check_password(new)
    # 正しい現在パスワード → 変更
    ok = http_client.post(
        "/members/password/",
        {"current_password": _PW, "new_password": new, "new_password_confirm": new},
    )
    assert ok.status_code == 302
    m.refresh_from_db()
    assert m.check_password(new)


@_PUBLIC_HOST
def test_unverified_banner_shown(http_client, db):
    _make_member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    res = http_client.get("/members/")
    assert res.status_code == 200
    assert "本人確認が未完了です" in res.content.decode("utf-8")


def test_member_verified_required_decorator(db):
    """ゲートの土台: 未認証=リダイレクト / 確認済=通過 / 未ログイン=ログインへ。"""
    from django.http import HttpResponse

    from members.decorators import member_verified_required

    @member_verified_required
    def view(request):
        return HttpResponse("ok")

    rf = RequestFactory()
    m = _make_member()

    req = rf.get("/x/")
    req.member = m  # 未認証
    r = view(req)
    assert r.status_code == 302 and r.url == "/members/verify-required/"

    m.email_verified_at = timezone.now()  # 確認済に
    req2 = rf.get("/x/")
    req2.member = m
    assert view(req2).status_code == 200

    req3 = rf.get("/x/")
    req3.member = None  # 未ログイン
    r3 = view(req3)
    assert r3.status_code == 302 and r3.url.startswith("/members/login/")


# ---- 居住国と郵便番号 (多通貨の前提整備) ----


@_PUBLIC_HOST
def test_register_overseas_allows_free_form_postal(http_client, db):
    """海外は郵便番号の書式を問わない (英 SW1A 1AA 等、国ごとに違うため)。"""
    res = http_client.post("/members/register/", _reg_data(country="GB", postal_code="SW1A 1AA"))
    assert res.status_code == 302
    m = Member.objects.get(email="viewer@example.com")
    assert m.country == "GB"
    assert m.postal_code == "SW1A 1AA"  # 正規化せず原文のまま


@_PUBLIC_HOST
def test_register_overseas_allows_empty_postal(http_client, db):
    """郵便番号を持たない国 (UAE・香港等) があるため、海外は空を許す。"""
    res = http_client.post("/members/register/", _reg_data(country="ZZ", postal_code=""))
    assert res.status_code == 302
    assert Member.objects.get(email="viewer@example.com").postal_code == ""


@_PUBLIC_HOST
def test_register_domestic_still_requires_postal(http_client, db):
    """国内は従来どおり必須。検証だけを国別にしても日本の要件は緩めない。"""
    res = http_client.post("/members/register/", _reg_data(country="JP", postal_code=""))
    assert res.status_code == 200  # 再表示 = 検証エラー
    assert not Member.objects.filter(email="viewer@example.com").exists()


@_PUBLIC_HOST
def test_register_domestic_rejects_non_seven_digits(http_client, db):
    res = http_client.post("/members/register/", _reg_data(country="JP", postal_code="SW1A 1AA"))
    assert res.status_code == 200
    assert not Member.objects.filter(email="viewer@example.com").exists()


@_PUBLIC_HOST
def test_register_and_edit_forms_render_country(http_client, db):
    """country は必須。入力欄を描いていないフォームは送信のたびに落ちる。"""
    body = http_client.get("/members/register/").content.decode("utf-8")
    assert 'name="country"' in body and "お住まいの国" in body
    _make_member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    edit = http_client.get("/members/edit/").content.decode("utf-8")
    assert 'name="country"' in edit


@_PUBLIC_HOST
def test_profile_edit_switches_to_overseas(http_client, db):
    """国内 → 海外へ移ると 7 桁の要求も外れる (書式は国ごとに違うため原文で保持)。"""
    m = _make_member()
    http_client.post("/members/login/", {"email": "m@example.com", "password": _PW})
    res = http_client.post(
        "/members/edit/",
        {
            "nickname": "tester",
            "birth_year": 1988,
            "birth_month": 6,
            "gender": "male",
            "country": "US",
            "postal_code": "94103-1234",
        },
    )
    assert res.status_code == 302
    m.refresh_from_db()
    assert m.country == "US" and m.postal_code == "94103-1234"
