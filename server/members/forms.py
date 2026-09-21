# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員のフォーム (登録/ログイン/プロフィール/パスワード変更)。

email/postal_code は正規化、password は既存 AUTH_PASSWORD_VALIDATORS を再利用 (validate_password)。
"""

from __future__ import annotations

import datetime
import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from core.forms import BootstrapFormMixin
from members.models import COUNTRY_CHOICES, Gender, Member

_POSTAL_RE = re.compile(r"^\d{7}$")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_postal(code: str) -> str:
    # 全角/半角ハイフン・空白を除去して半角数字 7 桁に寄せる
    return re.sub(r"[\s\-‐-―－ー]", "", code or "").strip()


def _validate_birth_year(year: int) -> int:
    current = datetime.date.today().year
    if year < 1900 or year > current:
        raise ValidationError("生年が正しくありません。")
    return year


def _validate_postal(code: str, country: str = "JP") -> str:
    """郵便番号を検証する。**国内は 7 桁必須、海外は任意で原文保持。**

    郵便番号を持たない国 (UAE・香港等) があるため海外を必須にはできない。書式も国ごとに
    違う (英 SW1A 1AA / 加 K1A 0B1 / 伯 01310-100) ので、日本以外は正規化しない。

    入力欄は 1 つのままで検証だけ分かれる。フォームのレイアウトを国で切り替えると
    実装も UI も複雑になるわりに、得られるのは「日本以外で 7 桁を要求しない」ことだけ。
    """
    if country != "JP":
        return (code or "").strip()[:16]
    code = normalize_postal(code)
    if not _POSTAL_RE.match(code):
        raise ValidationError("郵便番号は7桁の数字で入力してください (例: 1000001)。")
    return code


def _clean_country_postal(form: forms.BaseForm, cleaned: dict) -> None:
    """居住国に応じて郵便番号を検証し、`cleaned` を書き換える (登録/編集で共通)。

    郵便番号の検証は country に依存するため、フィールド単位 (clean_postal_code) では行えない
    (その時点では country が cleaned_data に入っている保証がない)。
    """
    country = cleaned.get("country") or "JP"
    if country == "JP" and not (cleaned.get("postal_code") or "").strip():
        form.add_error("postal_code", "郵便番号は必須です。")
    elif "postal_code" in cleaned:
        try:
            cleaned["postal_code"] = _validate_postal(cleaned["postal_code"], country)
        except ValidationError as e:
            form.add_error("postal_code", e)


class MemberRegistrationForm(BootstrapFormMixin, forms.Form):
    # 第 1 項目に置く。以降の入力 (郵便番号) の扱いがここで決まるため。
    country = forms.ChoiceField(label="お住まいの国", choices=COUNTRY_CHOICES, initial="JP")
    email = forms.EmailField(label="メールアドレス", max_length=254)
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput, strip=False)
    password_confirm = forms.CharField(
        label="パスワード(確認)", widget=forms.PasswordInput, strip=False
    )
    nickname = forms.CharField(label="ニックネーム", max_length=50)
    birth_year = forms.IntegerField(
        label="生年", widget=forms.NumberInput(attrs={"min": 1900, "placeholder": "1990"})
    )
    birth_month = forms.IntegerField(
        label="生月",
        min_value=1,
        max_value=12,
        widget=forms.NumberInput(attrs={"min": 1, "max": 12, "placeholder": "4"}),
    )
    gender = forms.ChoiceField(label="性別", choices=Gender.choices, initial=Gender.NO_ANSWER)
    postal_code = forms.CharField(
        label="郵便番号",
        max_length=16,
        required=False,
        help_text="日本にお住まいの場合は必須です。",
        widget=forms.TextInput(attrs={"placeholder": "1000001"}),
    )
    agree = forms.BooleanField(label="利用規約・プライバシーポリシーに同意する")

    def clean_email(self):
        email = normalize_email(self.cleaned_data["email"])
        if Member.objects.filter(email__iexact=email).exists():
            raise ValidationError("このメールアドレスは既に登録されています。")
        return email

    def clean_birth_year(self):
        return _validate_birth_year(self.cleaned_data["birth_year"])

    def clean(self):
        cleaned = super().clean()
        _clean_country_postal(self, cleaned)
        pw = cleaned.get("password")
        pw2 = cleaned.get("password_confirm")
        if pw and pw2 and pw != pw2:
            self.add_error("password_confirm", "パスワードが一致しません。")
        if pw:
            try:
                validate_password(pw)
            except ValidationError as e:
                self.add_error("password", e)
        return cleaned

    def create_member(self) -> Member:
        d = self.cleaned_data
        member = Member(
            email=d["email"],
            nickname=d["nickname"],
            birth_year=d["birth_year"],
            birth_month=d["birth_month"],
            gender=d["gender"],
            country=d["country"],
            postal_code=d["postal_code"],
        )
        member.set_password(d["password"])
        member.save()
        return member


class MemberLoginForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(label="メールアドレス", max_length=254)
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput, strip=False)


class MemberProfileForm(BootstrapFormMixin, forms.ModelForm):
    # 列に choices を持たせていないため、ModelForm 任せだと 2 文字のテキスト入力になる。
    # 提示する国の範囲はフォーム側の制約なので、ここで明示する。
    country = forms.ChoiceField(label="お住まいの国", choices=COUNTRY_CHOICES)

    class Meta:
        model = Member
        fields = ["nickname", "birth_year", "birth_month", "gender", "country", "postal_code"]
        labels = {
            "nickname": "ニックネーム",
            "birth_year": "生年",
            "birth_month": "生月",
            "gender": "性別",
            "postal_code": "郵便番号",
        }

    def clean_birth_year(self):
        return _validate_birth_year(self.cleaned_data["birth_year"])

    def clean(self):
        cleaned = super().clean()
        _clean_country_postal(self, cleaned)
        return cleaned


class MemberPasswordChangeForm(BootstrapFormMixin, forms.Form):
    current_password = forms.CharField(
        label="現在のパスワード", widget=forms.PasswordInput, strip=False
    )
    new_password = forms.CharField(
        label="新しいパスワード", widget=forms.PasswordInput, strip=False
    )
    new_password_confirm = forms.CharField(
        label="新しいパスワード(確認)", widget=forms.PasswordInput, strip=False
    )

    def __init__(self, member: Member, *args, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        pw = self.cleaned_data["current_password"]
        if not self.member.check_password(pw):
            raise ValidationError("現在のパスワードが正しくありません。")
        return pw

    def clean(self):
        cleaned = super().clean()
        pw = cleaned.get("new_password")
        pw2 = cleaned.get("new_password_confirm")
        if pw and pw2 and pw != pw2:
            self.add_error("new_password_confirm", "パスワードが一致しません。")
        if pw:
            try:
                validate_password(pw)
            except ValidationError as e:
                self.add_error("new_password", e)
        return cleaned

    def save(self) -> Member:
        self.member.set_password(self.cleaned_data["new_password"])
        self.member.save(update_fields=["password", "updated_at"])
        return self.member


class MemberEmailChangeForm(BootstrapFormMixin, forms.Form):
    new_email = forms.EmailField(label="新しいメールアドレス", max_length=254)
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput, strip=False)

    def __init__(self, member: Member, *args, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)

    def clean_new_email(self):
        email = normalize_email(self.cleaned_data["new_email"])
        if email == (self.member.email or "").lower():
            raise ValidationError("現在のメールアドレスと同じです。")
        if Member.objects.filter(email__iexact=email).exclude(pk=self.member.pk).exists():
            raise ValidationError("このメールアドレスは既に使われています。")
        return email

    def clean_password(self):
        if not self.member.check_password(self.cleaned_data["password"]):
            raise ValidationError("パスワードが正しくありません。")
        return self.cleaned_data["password"]


class MemberAccountDeleteForm(BootstrapFormMixin, forms.Form):
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput, strip=False)
    confirm = forms.BooleanField(label="退会して全データを削除することに同意します")

    def __init__(self, member: Member, *args, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)

    def clean_password(self):
        if not self.member.check_password(self.cleaned_data["password"]):
            raise ValidationError("パスワードが正しくありません。")
        return self.cleaned_data["password"]

    def clean(self):
        from fanclub.services import has_open_paid_membership
        from subscriptions.services import has_open_subscription

        cleaned = super().clean()
        # 退会は会員行を消すが Stripe の購読は解約しない。課金が続いたまま、受け取る側の Webhook も
        # 会員を引けず取りこぼすので、購読が残っている間は退会させない。
        if has_open_subscription(self.member) or has_open_paid_membership(self.member):
            raise ValidationError(
                "有効なサブスクリプションがあるため、退会できません。"
                "先にサブスクリプションを解約してから、あらためて退会してください。"
            )
        return cleaned


class PasswordResetRequestForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(label="メールアドレス", max_length=254)

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])


class PasswordResetConfirmForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(label="メールアドレス", max_length=254)
    code = forms.CharField(label="再設定コード", max_length=6)
    new_password = forms.CharField(
        label="新しいパスワード", widget=forms.PasswordInput, strip=False
    )
    new_password_confirm = forms.CharField(
        label="新しいパスワード(確認)", widget=forms.PasswordInput, strip=False
    )

    def clean_email(self):
        return normalize_email(self.cleaned_data["email"])

    def clean(self):
        cleaned = super().clean()
        pw = cleaned.get("new_password")
        pw2 = cleaned.get("new_password_confirm")
        if pw and pw2 and pw != pw2:
            self.add_error("new_password_confirm", "パスワードが一致しません。")
        if pw:
            try:
                validate_password(pw)
            except ValidationError as e:
                self.add_error("new_password", e)
        return cleaned
