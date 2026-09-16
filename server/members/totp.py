# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""会員の TOTP (認証アプリ) 2FA ヘルパ。

pyotp で RFC6238 の秘密生成/provisioning URI/検証、segno で QR を inline SVG 化 (Pillow 不要)。
秘密は Member.totp_secret に EncryptedTextField で at-rest 暗号化される (本モジュールは平文 str を扱う)。
"""

from __future__ import annotations

import time
from datetime import datetime

import pyotp
import segno

_ISSUER = "ICS-TV"
_INTERVAL = 30  # RFC6238 の time step 秒


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(account_name: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=_ISSUER)


def qr_svg(uri: str) -> str:
    """otpauth URI を inline SVG (テンプレに |safe で埋め込む) で返す。"""
    return segno.make(uri).svg_inline(scale=4)


def verify_totp(secret: str | None, code: str) -> bool:
    """現在コードを検証 (±1 ステップの時刻ずれを許容)。TOTP 設定確定など、リプレイ不問の用途。"""
    return matched_step(secret, code) is not None


def matched_step(secret: str | None, code: str, valid_window: int = 1) -> int | None:
    """コードが一致した time step 番号を返す (不一致は None)。ログインのリプレイ防止に使う。

    pyotp.verify は「一致したか」しか返さないため、±valid_window の各ステップを個別検証して
    一致したステップ index (= floor(for_time / interval)) を特定する。呼び出し側は直近に使った
    step 以下のコード再利用 (リプレイ・時刻ずれ窓での再送) を拒否できる。
    """
    code = (code or "").strip()
    if not secret or not code:
        return None
    totp = pyotp.TOTP(secret)
    now = time.time()
    for offset in range(-valid_window, valid_window + 1):
        ts = now + offset * _INTERVAL
        # naive local datetime を渡すと pyotp の timecode(mktime(local)) が int(ts/interval) に
        # 一致する (返す step と整合)。step の絶対値でなく単調性のみに依存するため十分。
        if totp.verify(code, for_time=datetime.fromtimestamp(ts), valid_window=0):
            return int(ts // _INTERVAL)
    return None
