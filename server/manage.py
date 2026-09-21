#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Django's command-line utility for administrative tasks."""

import os
import sys
from pathlib import Path

# 生成 proto (buf generate により icstv_proto/icstv/v1/ に出力) を import path へ。
# grpcserve コマンドが `from icstv.v1 import playout_pb2` を解決できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent / "icstv_proto"))


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
