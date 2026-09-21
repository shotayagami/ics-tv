# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CasparCgClient の AMCP プロトコル解釈テスト (実機検証の代替: fake AMCP サーバ)。

実 CasparCG が無くても、CRLF フレーミング・ステータスコード (200 複数行 / 201 単行 / 202 body無
/ 400) の解釈と connect→VERSION 生存確認を検証する。実機連携の中核ロジックをここで固める。
"""

from __future__ import annotations

import asyncio

from icstv_agent.caspar import CasparCgClient, parse_layer_states


def test_parse_layer_states_per_layer():
    xml = (
        "<channel><stage>"
        "<layer_10><background><producer>empty</producer></background>"
        "<foreground><producer>ffmpeg</producer><name>filler/5</name></foreground></layer_10>"
        "<layer_30><background><producer>empty</producer></background>"
        "<foreground><producer>html</producer><name>lbar/standard</name></foreground></layer_30>"
        "</stage></channel>"
    )
    got = parse_layer_states(xml, [10, 20, 30])
    assert got[10] == {"producer": "ffmpeg", "name": "filler/5"}  # 本線
    assert got[20] == {"producer": "empty", "name": None}  # ブロック無し=空
    assert got[30] == {
        "producer": "html",
        "name": "lbar/standard",
    }  # Lバー(layer10を拾わない)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """CasparCG 2.x 風の最小 AMCP 応答を返す fake サーバ。"""
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            break
        cmd = line.decode().strip()
        if not cmd:
            continue
        if cmd == "VERSION":
            writer.write(b"201 VERSION OK\r\n2.3.3 STABLE\r\n")
        elif cmd == "INFO":
            writer.write(b"200 INFO OK\r\n1 720p5994 PLAYING\r\n2 PAL PLAYING\r\n\r\n")
        elif cmd.split()[0] in ("PLAY", "LOADBG", "CG", "STOP", "CLEAR"):
            writer.write(b"202 OK\r\n")  # 成功・body 無
        else:
            writer.write(b"400 ERROR\r\n" + cmd.encode() + b"\r\n")
        await writer.drain()


async def _with_server(scenario):
    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # wait_for ガード: クライアント/サーバが応答しなくてもテストを吊らせない
        return await asyncio.wait_for(scenario(port), timeout=10)
    finally:
        server.close()  # wait_closed は待たない (ハンドラ完了待ちで吊るのを避ける)


def test_connect_then_version():
    async def scenario(port):
        c = CasparCgClient("127.0.0.1", port)
        assert await c.connect() is True  # connect は内部で VERSION 生存確認
        assert "2.3.3" in await c.version()
        await c.close()

    asyncio.run(_with_server(scenario))


def test_info_multiline_body():
    async def scenario(port):
        c = CasparCgClient("127.0.0.1", port)
        await c.connect()
        res = await c.amcp("INFO")
        assert res.code == 200
        assert res.is_success
        assert res.body == ["1 720p5994 PLAYING", "2 PAL PLAYING"]  # 空行終端まで
        await c.close()

    asyncio.run(_with_server(scenario))


def test_play_202_success_no_body():
    async def scenario(port):
        c = CasparCgClient("127.0.0.1", port)
        await c.connect()
        res = await c.amcp("PLAY 1-10")
        assert res.code == 202
        assert res.is_success
        assert res.body == []
        await c.close()

    asyncio.run(_with_server(scenario))


def test_cg_command_ok():
    async def scenario(port):
        c = CasparCgClient("127.0.0.1", port)
        await c.connect()
        res = await c.amcp('CG 1-50 ADD 0 "credit/sponsor" 1 "{}"')
        assert res.is_success
        await c.close()

    asyncio.run(_with_server(scenario))


def test_bad_command_400_not_success():
    async def scenario(port):
        c = CasparCgClient("127.0.0.1", port)
        await c.connect()
        res = await c.amcp("BOGUS xyz")
        assert res.code == 400
        assert not res.is_success
        await c.close()

    asyncio.run(_with_server(scenario))
