# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""静的配信 (WhiteNoise)。gunicorn 単体で /static/ が 200 + 正しい MIME で返ること。

DEBUG=False + gunicorn では Django/WSGI は静的を配信しないため、admin の CSS/JS が 404 になる
回帰を防ぐ。collectstatic で STATIC_ROOT を満たし WhiteNoise middleware が配信する。
"""

from __future__ import annotations

from django.core.management import call_command


def test_whitenoise_serves_admin_css(client):
    call_command("collectstatic", "--noinput", verbosity=0)
    res = client.get("/static/admin/css/base.css")
    assert res.status_code == 200
    assert "text/css" in res.headers.get("Content-Type", "")


def test_whitenoise_serves_admin_js(client):
    call_command("collectstatic", "--noinput", verbosity=0)
    res = client.get("/static/admin/js/theme.js")
    assert res.status_code == 200
    assert "javascript" in res.headers.get("Content-Type", "")


def test_whitenoise_middleware_configured(settings):
    # SecurityMiddleware の直後に WhiteNoise が居ること (配信規約)
    mw = settings.MIDDLEWARE
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in mw
    assert mw.index("whitenoise.middleware.WhiteNoiseMiddleware") == 1
