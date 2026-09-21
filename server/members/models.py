# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""公開サイト (tv.*) の視聴者会員 (会員管理 / サブスク課金の前段)。

決定: 視聴者アカウントは `django.contrib.auth.User` とは **分離** した独立モデルにする
(staff/admin/業者が載る auth_user に視聴者を混ぜない)。パスワードハッシュ/バリデータは
Django の hashers を再利用するが、ログインは `auth.login()`/User を使わず session["member_id"]
で運用する (members.auth / members.middleware)。

確認 (is_verified): メール検証 (email_verified_at) もしくは TOTP 確定 (totp_confirmed_at) の
どちらか 1 要素で確認済とみなす。コードを受け取れない利用者は **登録済み・未認証** のまま
利用でき、`member_verified_required` を着せた将来機能 (コメント等) だけが制限される土台。

統計利用: birth_year/birth_month・gender・postal_code は SQL 集計に使うため **平文** で持つ
(EncryptedTextField に入れると WHERE/集計ができない)。生年月日のフル DOB は持たない (PII 最小化)。
"""

from __future__ import annotations

import uuid

from django.contrib.auth.hashers import check_password as _check_password
from django.contrib.auth.hashers import make_password as _make_password
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower

from core.fields import EncryptedTextField


class Gender(models.TextChoices):
    MALE = "male", "男"
    FEMALE = "female", "女"
    NO_ANSWER = "no_answer", "無回答"


class TwoFactorMethod(models.TextChoices):
    NONE = "none", "なし"
    EMAIL = "email", "メール"
    TOTP = "totp", "認証アプリ"


# 提示する国の一覧。日本を先頭に置き、以降は表示名の五十音順。
# ここに無い国は登録できない = 対応範囲を明示的に制御する
# (税務・特商法の扱いを決めていない国を黙って受け入れないため)。
#
# **列そのものには choices を付けない。** 対応国を増やすたびに DB 上は何も変わらない
# マイグレーションが増えるのを避け、「提示する範囲」はフォーム側の制約として扱う。
COUNTRY_CHOICES = [
    ("JP", "日本"),
    ("AU", "オーストラリア"),
    ("CA", "カナダ"),
    ("KR", "韓国"),
    ("SG", "シンガポール"),
    ("TW", "台湾"),
    ("DE", "ドイツ"),
    ("FR", "フランス"),
    ("GB", "イギリス"),
    ("US", "アメリカ合衆国"),
    # ZZ は ISO 3166-1 がユーザー割当用に予約しているコード。列を alpha-2 のまま保てる。
    ("ZZ", "その他"),
]
COUNTRY_NAMES = dict(COUNTRY_CHOICES)


class Member(models.Model):
    email = models.EmailField(max_length=254)  # 一意は uq_member_email (Lower) で担保
    pending_email = models.EmailField(
        max_length=254, null=True, blank=True
    )  # メール変更中の新アドレス (確認コード通過で email へ確定)
    password = models.CharField(max_length=128)  # make_password 出力。平文は保存しない
    nickname = models.CharField(max_length=50)  # コメント機能で使用予定 (当面は非ユニーク)
    birth_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1900), MaxValueValidator(2200)]
    )  # 生年 (年齢層統計用)
    birth_month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )  # 生月
    gender = models.CharField(max_length=9, choices=Gender.choices, default=Gender.NO_ANSWER)
    # 居住国 (ISO 3166-1 alpha-2)。既定は JP。
    #
    # **通貨の判別には使えない。** Stripe の通貨は課金した Price が決めるため、
    # ドイツ在住の会員に JPY を課金することも、日本在住の会員に USD を課金することもできる。
    # この列の用途は「どの Price を提示するか」「税務・特商法の扱い」の判断であって、
    # 台帳の通貨は各金額行の currency 列が持つ。
    country = models.CharField(max_length=2, default="JP")
    # 郵便番号。**国内は 7 桁必須、海外は任意**。郵便番号を持たない国 (UAE・香港等) が
    # あるため海外を必須にはできない。書式も国ごとに違う (英 SW1A 1AA / 加 K1A 0B1 /
    # 伯 01310-100) ので、日本以外は正規化せず原文のまま保持する。
    # 入力欄は 1 つのままで、検証だけ国別に分かれる (登録フォームの分岐を避けるため)。
    postal_code = models.CharField(max_length=16, blank=True, default="")
    is_active = models.BooleanField(default=True)  # soft-disable / 将来の凍結

    # --- 本人確認 (どちらか1要素で is_verified=True) ---
    email_verified_at = models.DateTimeField(null=True, blank=True)
    totp_secret = EncryptedTextField(null=True, blank=True)  # base32 秘密。Fernet at-rest
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    totp_last_step = models.BigIntegerField(
        null=True, blank=True
    )  # 直近にログイン認証で使った TOTP time step。同ステップ以下の再利用(リプレイ)を拒否
    two_factor_method = models.CharField(
        max_length=5, choices=TwoFactorMethod.choices, default=TwoFactorMethod.NONE
    )  # ログイン時に要求する第2要素 (none=パスワードのみ)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "member"
        constraints = [
            models.UniqueConstraint(Lower("email"), name="uq_member_email"),
            models.CheckConstraint(
                name="chk_member_birth_month",
                condition=models.Q(birth_month__gte=1, birth_month__lte=12),
            ),
        ]
        indexes = [
            # 統計集計 (年齢層×性別×地域) 用の軽いインデックス
            models.Index(fields=["birth_year"], name="idx_member_birth_year"),
        ]

    def __str__(self):
        return f"{self.nickname} <{self.email}>"

    # --- パスワード (Django hashers を再利用。User には載せない) ---
    def set_password(self, raw_password: str) -> None:
        self.password = _make_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return _check_password(raw_password, self.password)

    @property
    def country_label(self) -> str:
        """居住国の表示名。列に choices を付けていないので自前で引く。

        一覧から外した国のコードが残っていても表示は壊さない (コードをそのまま出す)。
        """
        return COUNTRY_NAMES.get(self.country, self.country)

    @property
    def is_verified(self) -> bool:
        """メール検証済 もしくは TOTP 確定済 なら確認済。"""
        return self.email_verified_at is not None or self.totp_confirmed_at is not None

    def age(self, now=None) -> int:
        """生年月からの満年齢 (#BILL-02 年齢ゲート用)。

        生年月のみ保持 (PII 最小化・DOB のフル日付は持たない) のため月精度。誕生「日」は
        不明なので誕生月の到来で歳を取る近似とする。年齢制限の視聴可否判定に使う。
        """
        from django.utils import timezone

        lt = timezone.localtime(now or timezone.now())
        years = lt.year - self.birth_year
        if lt.month < self.birth_month:
            years -= 1
        return years


class Comment(models.Model):
    """会員のチャンネルコメント (ライブチャット風)。確認済み会員のみ投稿・全員閲覧。

    member 退会で CASCADE 削除 (PII)。本人/staff の非表示は deleted_at (soft delete) で、
    表示は deleted_at IS NULL のみ。program_title は投稿時の放送中番組名スナップショット
    (transient な Program に結合せず「何に対して」を残す)。
    """

    channel = models.ForeignKey("core.Channel", on_delete=models.CASCADE, related_name="comments")
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=500)
    program_title = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)  # 本人/staff の非表示 (soft delete)

    class Meta:
        db_table = "member_comment"
        indexes = [models.Index(fields=["channel", "-created_at"], name="idx_comment_ch_created")]

    def __str__(self):
        return f"comment#{self.pk} ch={self.channel_id} m={self.member_id}"


class Favorite(models.Model):
    """会員のマイリスト (あとで見る)。番組(放映)単位で保存 (#PERS-01)。

    番組詳細/見逃しから登録し、専用一覧から再生・再訪する。member 退会で CASCADE。
    番組削除でも CASCADE (放映が消えればリストからも消える)。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="favorites")
    program = models.ForeignKey("scheduling.Program", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "member_favorite"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "program"], name="uq_favorite_member_program"
            ),
        ]
        indexes = [models.Index(fields=["member", "-created_at"], name="idx_favorite_member")]

    def __str__(self):
        return f"fav m={self.member_id} p={self.program_id}"


class ChannelFavorite(models.Model):
    """会員のお気に入り (ピン留め) チャンネル (#EPG-04)。

    ピン留めした ch をトップのチャンネルカード/視聴ページのタブで先頭に並べる。
    member 退会/チャンネル削除で CASCADE。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="channel_favorites")
    channel = models.ForeignKey("core.Channel", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "member_channel_favorite"
        constraints = [
            models.UniqueConstraint(fields=["member", "channel"], name="uq_chanfav_member_channel"),
        ]
        indexes = [models.Index(fields=["member", "-created_at"], name="idx_chanfav_member")]

    def __str__(self):
        return f"chanfav m={self.member_id} c={self.channel_id}"


class Reminder(models.Model):
    """番組開始前のリマインド通知予約 (#EPG-03)。開始 lead 分前にメール送信。

    notified_at で一度だけ送信 (二重送信防止)。member 退会/番組削除で CASCADE。
    送信は Celery Beat の members.tasks.send_due_reminders が拾う (開始済みは送らない)。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="reminders")
    program = models.ForeignKey("scheduling.Program", on_delete=models.CASCADE, related_name="+")
    notified_at = models.DateTimeField(null=True, blank=True)  # 送信済み時刻 (二重送信防止)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "member_reminder"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "program"], name="uq_reminder_member_program"
            ),
        ]
        indexes = [
            # 「未送信 × 開始時刻」で due を引く周期タスク用
            models.Index(fields=["notified_at", "program"], name="idx_reminder_due"),
        ]

    def __str__(self):
        return f"reminder m={self.member_id} p={self.program_id}"


class WatchHistory(models.Model):
    """会員の視聴履歴 + 続きから再生位置 (#PERS-02)。VOD 再生位置を会員ごとに保持。

    プレイヤーが周期/一時停止/離脱で position_ms を upsert。completed=末尾近くまで見た。
    member 退会/番組削除で CASCADE。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="watch_history")
    program = models.ForeignKey("scheduling.Program", on_delete=models.CASCADE, related_name="+")
    position_ms = models.BigIntegerField(default=0)  # 続きから再生位置
    duration_ms = models.BigIntegerField(null=True, blank=True)  # 進捗率算出用 (任意)
    completed = models.BooleanField(default=False)  # 末尾近くまで視聴済み
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "member_watch_history"
        constraints = [
            models.UniqueConstraint(fields=["member", "program"], name="uq_watch_member_program"),
        ]
        indexes = [models.Index(fields=["member", "-updated_at"], name="idx_watch_member")]

    def __str__(self):
        return f"watch m={self.member_id} p={self.program_id} {self.position_ms}ms"


class CodePurpose(models.TextChoices):
    EMAIL_VERIFY = "email_verify", "メール確認"
    LOGIN_2FA = "login_2fa", "ログイン2段階"
    PASSWORD_RESET = "password_reset", "パスワード再設定"  # pragma: allowlist secret
    EMAIL_CHANGE = "email_change", "メールアドレス変更"


class AuthThrottle(models.Model):
    """認証系エンドポイントの Cookie 非依存レート制限カウンタ (H-1 / M-1)。

    scope+key ごとに 1 行。key はメール (HMAC ハッシュ・平文非保存) か送信元 IP。
    ウィンドウ内の失敗回数を数え、上限到達でウィンドウ満了まで locked。従来 session に
    持っていた失敗回数を DB へ移し、Cookie を捨てるだけの総当たり回避を無効化する。
    集計/検証ロジックは members.throttle。updated_at で古い行を掃除できる。
    """

    scope = models.CharField(max_length=32)  # members.throttle の scope 定数
    key = models.CharField(max_length=128)  # email の HMAC ハッシュ or 送信元 IP
    fail_count = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField()  # 現ウィンドウの起点 (満了で巻き直す)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "member_auth_throttle"
        constraints = [
            models.UniqueConstraint(fields=["scope", "key"], name="uq_auththrottle_scope_key"),
        ]
        indexes = [models.Index(fields=["updated_at"], name="idx_auththrottle_updated")]

    def __str__(self):
        return f"throttle {self.scope}:{self.key[:12]} n={self.fail_count}"


class MemberEmailCode(models.Model):
    """会員のメール確認/2FA コード。

    code は 6 桁。make_password でハッシュ保管し、平文は DB に残さない (メール本文にのみ出る)。
    有効 = consumed_at が NULL かつ expires_at>now かつ attempts<上限。誤入力で attempts++、
    上限到達で当該コードは死ぬ (再送が必要) → 6 桁ブルートフォースを抑止。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="email_codes")
    purpose = models.CharField(max_length=16, choices=CodePurpose.choices)
    code_hash = models.CharField(max_length=128)  # make_password(6桁)。平文は保存しない
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)  # 送信レート制限にも使う

    class Meta:
        db_table = "member_email_code"
        indexes = [models.Index(fields=["member", "purpose"], name="idx_member_code_mp")]

    def __str__(self):
        return f"code#{self.pk} {self.purpose} m={self.member_id}"


class MemberApiToken(models.Model):
    """モバイルアプリ用の Bearer トークン (#MOBILE-01)。

    Web は session cookie + CSRF のままで、これはネイティブアプリ専用の第2の入口。cookie jar は
    Android/iOS のどちらでも一級市民ではなく、2FA を挟む HTML リダイレクト連鎖もアプリからは
    追えないため、別レイヤを立てている。

    平文は発行時のレスポンスにしか出ず、DB には SHA-256 のみを持つ (メール確認コードが
    make_password で保管しているのと同じ方針だが、こちらは総当たり不可能な 256bit 乱数なので
    低速ハッシュではなく SHA-256 で足りる。逆にログイン毎に検索するため速度が要る)。
    """

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="api_tokens")
    token_hash = models.CharField(max_length=64, unique=True)  # sha256 hex。平文は保存しない
    device_label = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "member_api_token"
        indexes = [models.Index(fields=["member", "revoked_at"], name="idx_member_token_mr")]

    def __str__(self):
        return f"token#{self.pk} m={self.member_id} {self.device_label}"


class MemberLoginChallenge(models.Model):
    """トークンログインの第2要素 待ち状態 (#MOBILE-01)。

    HTML 版は session に pending を持つ (members.auth.start_pending_2fa) が、アプリには
    session が無いため同じ役割をこのレコードが担う。challenge_id を返し、次の呼び出しで
    コードと一緒に送り返してもらう。

    session 版と同様、第2要素を通すまでトークンは発行しない (中途半端な状態でログイン扱いに
    しない)。単回使用かつ短命で、消費済み/期限切れは復活しない。
    """

    challenge_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="login_challenges")
    method = models.CharField(max_length=8)  # "totp" / "email"
    device_label = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "member_login_challenge"
        indexes = [models.Index(fields=["member"], name="idx_member_challenge_m")]

    def __str__(self):
        return f"challenge#{self.pk} {self.method} m={self.member_id}"
