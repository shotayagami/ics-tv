# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""loudnorm 2-pass のフィルタ生成/measured 解析 + R2 共用クライアントのテスト (#5 D4/D5)。

ffmpeg/boto3 は呼ばず、純粋ロジック (フィルタ文字列・JSON 解析) と env 読みを検証。
"""

from __future__ import annotations

from pathlib import Path

from medialib import mezz

_MEAS = {
    "input_i": "-23.45",
    "input_tp": "-5.20",
    "input_lra": "7.10",
    "input_thresh": "-33.60",
    "output_i": "-14.00",
    "target_offset": "0.30",
}


def test_loudnorm_filter_embeds_measured_and_targets():
    # loudnorm ロジックは mezz へ移設済 (normalize と watcher で共有)。spec が目標値を持つ。
    f = mezz.loudnorm_filter(_MEAS, mezz.spec_from_env())
    assert f.startswith("loudnorm=I=-14:LRA=11:TP=-1.5")
    assert "measured_I=-23.45" in f
    assert "measured_TP=-5.20" in f
    assert "measured_LRA=7.10" in f
    assert "measured_thresh=-33.60" in f
    assert "offset=0.30" in f
    assert f.endswith("linear=true")


def test_loudnorm_first_pass_parses_trailing_json(monkeypatch):
    stderr = (
        b"ffmpeg version ...\n[Parsed_loudnorm_0 @ 0x55] \n"
        b'{\n\t"input_i" : "-23.45",\n\t"input_tp" : "-5.20",\n'
        b'\t"input_lra" : "7.10",\n\t"input_thresh" : "-33.60",\n'
        b'\t"output_i" : "-14.00",\n\t"target_offset" : "0.30"\n}\n'
    )

    class _R:
        def __init__(self):
            self.stderr = stderr
            self.returncode = 0

    monkeypatch.setattr(mezz.subprocess, "run", lambda *a, **k: _R())
    meas = mezz.loudnorm_first_pass(Path("x.mp4"), mezz.spec_from_env())
    assert meas is not None
    assert meas["input_i"] == "-23.45"
    assert meas["target_offset"] == "0.30"


def test_loudnorm_first_pass_returns_none_without_json(monkeypatch):
    class _R:
        stderr = b"ffmpeg: no audio stream; loudnorm skipped"
        returncode = 1

    monkeypatch.setattr(mezz.subprocess, "run", lambda *a, **k: _R())
    assert mezz.loudnorm_first_pass(Path("x.mp4"), mezz.spec_from_env()) is None


def test_r2_bucket_reads_env(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "icstv-test-bucket")
    from core import r2

    assert r2.bucket() == "icstv-test-bucket"
