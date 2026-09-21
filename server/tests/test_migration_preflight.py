# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline observations for migration planning; no broker or database writes."""

from cryptography.fernet import Fernet
from django.test import override_settings

from config.celery import app
from core.fields import EncryptedTextField


def test_registration_and_reconciliation_need_offload_consumer():
    for task in ("medialib.tasks.dispatch_normalize", "medialib.tasks.reconcile_normalize_offload"):
        assert app.amqp.router.route({}, task)["queue"].name == "offload"
    assert app.amqp.router.route({}, "medialib.tasks.normalize_asset")["queue"].name == "normalize"
    assert app.amqp.router.route({}, "medialib.tasks.transcribe_asset")["queue"].name == "captions"


def test_synthetic_encryption_compatibility():
    field = EncryptedTextField()
    expected = "synthetic-fixture-value"
    key = Fernet.generate_key().decode()
    with override_settings(ICSTV_FIELD_ENCRYPTION_KEY=key):
        stored = field.get_prep_value(expected)
        assert stored != expected and expected not in stored
        assert field.from_db_value(stored, None, None) == expected
        assert field.get_prep_value(stored) == stored
    for wrong in (Fernet.generate_key().decode(), ""):
        with override_settings(ICSTV_FIELD_ENCRYPTION_KEY=wrong):
            assert field.from_db_value(stored, None, None) == stored
            assert field.from_db_value(stored, None, None) != expected
            assert field.get_prep_value(stored) == stored
    with override_settings(ICSTV_FIELD_ENCRYPTION_KEY=""):
        assert field.get_prep_value(expected) == expected
