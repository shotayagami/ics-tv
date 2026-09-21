# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""字幕自動生成 (#ADMIN-04 / PLAYER-04・決定#24): VTT 整形・生成フロー・投入ゲート。"""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace
from unittest import mock

from medialib import captions, tasks
from medialib.models import Asset, AssetKind, CaptionStatus, NormalizeStatus


def _seg(start, end, text):
    return SimpleNamespace(start=start, end=end, text=text)


# ---- _fmt_ts ----


def test_fmt_ts_zero():
    assert captions._fmt_ts(0) == "00:00:00.000"


def test_fmt_ts_hms_ms():
    assert captions._fmt_ts(3661.5) == "01:01:01.500"


def test_fmt_ts_negative_clamped():
    assert captions._fmt_ts(-3) == "00:00:00.000"


# ---- build_vtt ----


def test_build_vtt_formats_cues():
    vtt = captions.build_vtt([_seg(0.0, 1.5, "こんにちは"), _seg(1.5, 3.0, " 世界 ")])
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.500\nこんにちは\n" in vtt
    assert "00:00:01.500 --> 00:00:03.000\n世界\n" in vtt  # 前後空白は trim


def test_build_vtt_skips_empty_text():
    vtt = captions.build_vtt([_seg(0.0, 1.0, "  "), _seg(1.0, 2.0, "本文")])
    assert "本文" in vtt
    assert vtt.count("-->") == 1  # 空白セグメントは cue にしない


def test_build_vtt_no_segments_is_valid_empty():
    vtt = captions.build_vtt([])
    assert vtt.startswith("WEBVTT") and "-->" not in vtt  # ヘッダのみ・cue 無し


def test_caption_key():
    a = Asset(id=42, kind=AssetKind.PROGRAM)
    assert captions.caption_key(a, "ja") == "captions/program/42.ja.vtt"


# ---- transcribe() ----


def test_transcribe_happy_path(asset_ready, db):
    fake_model = mock.Mock()
    fake_model.transcribe.return_value = ([_seg(0.0, 1.0, "テスト字幕")], None)
    with (
        mock.patch("medialib.captions._extract_audio"),
        mock.patch("medialib.captions._get_model", return_value=fake_model),
        mock.patch("core.r2.presign_get", return_value="https://signed/mezz"),
        mock.patch("core.r2.put_object") as put,
    ):
        captions.transcribe(asset_ready)

    asset_ready.refresh_from_db()
    assert asset_ready.caption_status == CaptionStatus.READY
    assert asset_ready.caption_r2_key == f"captions/program/{asset_ready.id}.ja.vtt"
    assert asset_ready.caption_lang == "ja"
    key, body, content_type = put.call_args.args
    assert key == asset_ready.caption_r2_key
    assert content_type.startswith("text/vtt")
    assert b"WEBVTT" in body and "テスト字幕".encode() in body


def test_transcribe_skips_non_program(db):
    cm = Asset.objects.create(
        kind=AssetKind.CM,
        title="cm",
        r2_key="mezzanine/cm/1.mp4",
        normalize_status=NormalizeStatus.READY,
    )
    with mock.patch("medialib.captions._get_model") as gm:
        captions.transcribe(cm)
    gm.assert_not_called()
    cm.refresh_from_db()
    assert cm.caption_status == CaptionStatus.NONE  # 手つかず


def test_transcribe_skips_when_ready_caption_exists(db):
    """READY の字幕が既にある素材は whisper で上書きしない (最後の砦・決定#26)。

    slidecast の台本由来 VTT 先付けを、混在バージョン窓の旧 normalize pod がガード無しで
    enqueue しても潰さないための防御 (dev 実発生 2026-07-09 の再発防止)。
    """
    a = Asset.objects.create(
        kind=AssetKind.PROGRAM,
        title="prefetched",
        r2_key="mezzanine/program/9.mp4",
        normalize_status=NormalizeStatus.READY,
        caption_status=CaptionStatus.READY,
        caption_r2_key="captions/program/9.ja.vtt",
    )
    with (
        mock.patch("medialib.captions._get_model") as gm,
        mock.patch("core.r2.put_object") as put,
    ):
        captions.transcribe(a)
    gm.assert_not_called()
    put.assert_not_called()
    a.refresh_from_db()
    assert a.caption_status == CaptionStatus.READY  # PROCESSING/FAILED に落とさない
    assert a.caption_r2_key == "captions/program/9.ja.vtt"


def test_transcribe_failure_marks_failed(asset_ready, db):
    with (
        mock.patch("medialib.captions._extract_audio", side_effect=RuntimeError("boom")),
        mock.patch("core.r2.presign_get", return_value="https://signed/mezz"),
        contextlib.suppress(RuntimeError),
    ):
        captions.transcribe(asset_ready)
    asset_ready.refresh_from_db()
    assert asset_ready.caption_status == CaptionStatus.FAILED
    assert "boom" in (asset_ready.caption_error or "")


# ---- _maybe_enqueue_captions (投入ゲート) ----


def test_enqueue_captions_program_ready(asset_ready):
    with mock.patch("medialib.tasks.transcribe_asset.delay") as delay:
        tasks._maybe_enqueue_captions(asset_ready)
    delay.assert_called_once_with(asset_ready.id)


def test_enqueue_captions_skips_non_program(db):
    cm = Asset.objects.create(kind=AssetKind.CM, title="cm", normalize_status=NormalizeStatus.READY)
    with mock.patch("medialib.tasks.transcribe_asset.delay") as delay:
        tasks._maybe_enqueue_captions(cm)
    delay.assert_not_called()


def test_enqueue_captions_skips_not_ready(db):
    a = Asset.objects.create(
        kind=AssetKind.PROGRAM, title="p", normalize_status=NormalizeStatus.PROCESSING
    )
    with mock.patch("medialib.tasks.transcribe_asset.delay") as delay:
        tasks._maybe_enqueue_captions(a)
    delay.assert_not_called()


def test_enqueue_captions_killswitch(asset_ready):
    with (
        mock.patch.dict(os.environ, {"CAPTIONS_ENABLED": "false"}),
        mock.patch("medialib.tasks.transcribe_asset.delay") as delay,
    ):
        tasks._maybe_enqueue_captions(asset_ready)
    delay.assert_not_called()
