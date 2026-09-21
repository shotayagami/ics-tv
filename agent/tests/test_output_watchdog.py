# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""出力 watchdog: foreground 解析と 黒(empty)/フリーズ 判定 (#7 出力健全の盲点)。"""

from __future__ import annotations

from icstv_agent.caspar import output_is_bad, parse_foreground

_PLAYING = (
    "<channel><stage><layer_10><foreground>"
    "<producer>ffmpeg</producer><name>filler/2</name>"
    "<time>76.700</time><time>308.500</time><paused>false</paused>"
    "</foreground><background><producer>empty</producer></background>"
    "</layer_10></stage></channel>"
)
_EMPTY = (
    "<channel><stage><layer_10><foreground>"
    "<producer>empty</producer></foreground></layer_10></stage></channel>"
)
_PAUSED = _PLAYING.replace("<paused>false</paused>", "<paused>true</paused>")
# casparcg 再起動直後: 本線が未占有で <layer_10> ブロックごと出ない (2026-08-21 実機採取)。
_LAYER_ABSENT = "<channel><format>720p6000</format><stage><layer></layer></stage></channel>"
# 本線は空だが L バー層だけ生きている: 層で絞らないと L バーを本線と誤認する。
_OTHER_LAYER_ONLY = (
    "<channel><stage><layer><layer_30><foreground>"
    "<producer>html</producer><name>lbar/standard</name><time>12.5</time>"
    "<paused>false</paused></foreground></layer_30></layer></stage></channel>"
)


def test_parse_foreground_playing():
    fg = parse_foreground(_PLAYING, 10)
    assert fg == {
        "producer": "ffmpeg",
        "name": "filler/2",
        "time": 76.7,
        "paused": False,
    }


def test_parse_foreground_empty():
    fg = parse_foreground(_EMPTY, 10)
    assert fg is not None
    assert fg["producer"] == "empty"
    assert fg["time"] is None


def test_parse_foreground_not_channel_xml():
    # channel XML と解釈できない = 判定不能 (None)
    assert parse_foreground("<channel></channel>", 10) is None


def test_parse_foreground_layer_absent_is_empty():
    """未占有の層は <layer_NN> ごと出ない。判定不能ではなく黒 (#7 出力 watchdog の盲点)。

    ここを None にしていたため、2026-08-21 の計画的リサイクル後に本線が 35 分黒のまま
    watchdog が一度も発火しなかった。
    """
    fg = parse_foreground(_LAYER_ABSENT, 10)
    assert fg is not None
    assert fg["producer"] == "empty"


def test_parse_foreground_scoped_to_requested_layer():
    # L バー層が生きていても本線 (10) は黒と判定する
    fg = parse_foreground(_OTHER_LAYER_ONLY, 10)
    assert fg is not None
    assert fg["producer"] == "empty"
    assert fg["name"] is None
    # 要求した層を見れば L バーの状態は取れる
    lbar = parse_foreground(_OTHER_LAYER_ONLY, 30)
    assert lbar is not None
    assert lbar["name"] == "lbar/standard"


def test_output_bad_empty():
    # 黒 (foreground empty) は前回状態に関係なく bad
    assert output_is_bad(parse_foreground(_EMPTY, 10), "filler/2", 10.0) is True


def test_output_bad_frozen():
    # 同一 clip で time が前回と不変 (76.7→76.7) → フリーズ
    assert output_is_bad(parse_foreground(_PLAYING, 10), "filler/2", 76.7) is True


def test_output_ok_advancing():
    # time が進んでいる → 健全
    assert output_is_bad(parse_foreground(_PLAYING, 10), "filler/2", 70.0) is False


def test_output_ok_paused():
    # 一時停止は意図的とみなし bad 扱いしない
    assert output_is_bad(parse_foreground(_PAUSED, 10), "filler/2", 76.7) is False


def test_output_ok_clip_changed():
    # 別 clip / loop 巻き戻し直後は time 比較せず False (誤検知防止)
    assert output_is_bad(parse_foreground(_PLAYING, 10), "asset/9", 76.7) is False


def test_output_ok_info_failed():
    # INFO 取得失敗 (None) は判定不能で False
    assert output_is_bad(None, "filler/2", 76.7) is False


def test_output_bad_layer_absent():
    # casparcg 再起動直後の「層ごと無い」黒を bad として拾う (再 take の発火点)
    assert output_is_bad(parse_foreground(_LAYER_ABSENT, 10), "filler/2", 76.7) is True
