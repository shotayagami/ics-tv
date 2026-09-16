# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""medialib.mezz (エンコード仕様 + ffmpeg コマンド構築 + spec 検証) の単体テスト。

DB 非依存の純粋モジュールなので django_db 不要。
"""

from __future__ import annotations

import pytest

from medialib import mezz


def _valid_spec() -> dict:
    return {
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "vbitrate": "16M",
        "abitrate": "192k",
        "arate": "48000",
        "vcodec": "libx264",
        "preset": "veryfast",
        "acodec": "aac",
        "pix_fmt": "yuv420p",
        "profile": "high",
        "container": "mp4",
        "loudnorm_i": "-14",
        "loudnorm_lra": "11",
        "loudnorm_tp": "-1.5",
    }


def test_validate_spec_accepts_valid():
    out = mezz.validate_spec(_valid_spec())
    assert out["width"] == 1920 and out["vcodec"] == "libx264"


@pytest.mark.parametrize(
    "field,bad",
    [
        ("width", 0),
        ("width", 99999),
        ("width", "1920"),  # 文字列は不可 (数値のみ)
        ("height", -1),
        ("fps", 0),
        ("vcodec", "libx265"),  # ホワイトリスト外
        ("vcodec", "libx264; rm -rf /"),  # 注入試行
        ("preset", "evil"),
        ("acodec", "mp3"),
        ("pix_fmt", "rgb24"),
        ("profile", "insane"),
        ("container", "mkv"),
        ("vbitrate", "16M -filter_complex x"),  # 引数密輸試行
        ("vbitrate", "16Gigs"),
        ("abitrate", ""),
        ("arate", "abc"),
        ("arate", "999999"),  # 上限外
        ("loudnorm_i", "-14; evil"),
    ],
)
def test_validate_spec_rejects_bad(field, bad):
    spec = _valid_spec()
    spec[field] = bad
    with pytest.raises(ValueError):
        mezz.validate_spec(spec)


def test_validate_spec_rejects_bool_width():
    # bool は int のサブクラスなので明示的に弾く (True==1 で通ってしまわないこと)。
    spec = _valid_spec()
    spec["width"] = True
    with pytest.raises(ValueError):
        mezz.validate_spec(spec)


def test_build_normalize_cmd_is_argv_list_no_shell():
    spec = _valid_spec()
    meas = {
        "input_i": "-20",
        "input_tp": "-1",
        "input_lra": "7",
        "input_thresh": "-30",
        "target_offset": "0.1",
    }
    cmd = mezz.build_normalize_cmd("in.mov", "out.mp4", spec, meas)
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-b:v") + 1] == "16M"
    assert cmd[-1] == "out.mp4"
    # loudnorm 2-pass フィルタが measured 値を含む
    af = cmd[cmd.index("-af") + 1]
    assert "measured_I=-20" in af and "linear=true" in af


def test_build_normalize_cmd_without_meas_omits_loudnorm():
    cmd = mezz.build_normalize_cmd("in.mov", "out.mp4", _valid_spec(), None)
    assert "-af" not in cmd
    assert cmd[cmd.index("-ar") + 1] == "48000"


def test_build_passthrough_cmd_copies_video():
    cmd = mezz.build_passthrough_cmd("in.mov", "out.mp4", _valid_spec(), None)
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "copy"  # meas 無しは音声もコピー


def test_fps_from_str():
    assert mezz.fps_from_str("60/1") == 60.0
    assert mezz.fps_from_str("30000/1001") == round(30000 / 1001, 3)
    assert mezz.fps_from_str("0/0") is None
    assert mezz.fps_from_str(None) is None
