# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CasparCG AMCP TCP クライアント (port 5250)。docs/casparcg.md §5.1。

- 1 connection = 1 lock で直列化 (CasparCG 側は順序保証のため 1 行ずつ)
- CRLF 終端
- ステータスコード解釈: 200 (複数行/空行終端) / 201 (1 行) / 202 (header のみ) / 4xx / 5xx
- 公開する操作: loadbg / play / stop / clear / version / amcp(raw)
- 健康確認は version() 経由 (PING は版依存のため使わない)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CRLF = b"\r\n"


@dataclass(frozen=True)
class AmcpResult:
    code: int
    header: str
    body: list[str]

    @property
    def klass(self) -> int:
        return self.code // 100

    @property
    def is_success(self) -> bool:
        return self.klass == 2


class AmcpError(Exception):
    """AMCP コマンドが 4xx/5xx を返した。"""

    def __init__(self, result: AmcpResult, command: str) -> None:
        self.result = result
        self.command = command
        super().__init__(f"AMCP {command!r} -> {result.header}")


class CasparCgClient:
    def __init__(self, host: str, port: int = 5250) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """AMCP TCP セッションを開き、VERSION で生存確認。失敗しても例外は投げず False。"""
        try:
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
            self._connected = True
        except OSError as e:
            logger.warning(
                "CasparCG connect failed %s:%d (%s) — dispatch loop は idle のまま継続",
                self._host,
                self._port,
                e,
            )
            return False
        try:
            version = await self.version()
            logger.info("CasparCG connected %s:%d version=%s", self._host, self._port, version)
            return True
        except Exception:
            logger.exception("CasparCG VERSION check failed after connect")
            await self.close()
            return False

    async def amcp(self, command: str) -> AmcpResult:
        """生 AMCP コマンドを 1 行送って応答を返す。直列化。"""
        async with self._lock:
            return await self._send(command)

    async def _send(self, command: str) -> AmcpResult:
        if not self._connected or self._writer is None or self._reader is None:
            raise ConnectionError("CasparCG not connected")
        try:
            self._writer.write(command.encode("utf-8") + _CRLF)
            await self._writer.drain()
            return await self._read_response(command)
        except (OSError, asyncio.IncompleteReadError):
            self._connected = False
            raise

    async def _read_response(self, command: str) -> AmcpResult:
        assert self._reader is not None
        header_line = await self._reader.readuntil(_CRLF)
        header = header_line.decode("utf-8", errors="replace").rstrip("\r\n")
        code = _parse_code(header, command)
        body: list[str] = []
        if code == 200:
            # 複数行データ、空行終端
            while True:
                line = (await self._reader.readuntil(_CRLF)).decode("utf-8", errors="replace")
                line = line.rstrip("\r\n")
                if not line:
                    break
                body.append(line)
        elif code in (201, 400):
            line = (await self._reader.readuntil(_CRLF)).decode("utf-8", errors="replace")
            body.append(line.rstrip("\r\n"))
        return AmcpResult(code=code, header=header, body=body)

    # ---- 高水準コマンド ----

    async def version(self) -> str:
        """`VERSION` の応答 (201 single-line)。"""
        result = await self.amcp("VERSION")
        if not result.is_success:
            raise AmcpError(result, "VERSION")
        return result.body[0] if result.body else ""

    async def loadbg(
        self,
        channel: int,
        layer: int,
        clip: str,
        *,
        seek: int | None = None,
        length: int | None = None,
        loop: bool = False,
        mix_frames: int | None = None,
        auto: bool = False,
    ) -> AmcpResult:
        """LOADBG: 背面へロードのみ (前面は触らない)。docs/casparcg.md §1.2 のトークン順序を尊重。"""
        parts = [f'LOADBG {channel}-{layer} "{_escape(clip)}"']
        if loop:
            parts.append("LOOP")
        if mix_frames is not None and mix_frames > 0:
            parts.extend(["MIX", str(mix_frames)])
        if seek is not None:
            parts.extend(["SEEK", str(seek)])
        if length is not None:
            parts.extend(["LENGTH", str(length)])
        if auto:
            parts.append("AUTO")
        return await self.amcp(" ".join(parts))

    async def play(
        self,
        channel: int,
        layer: int,
        *,
        clip: str | None = None,
        loop: bool = False,
    ) -> AmcpResult:
        """PLAY: 引数なしで「直前 LOADBG した背面をテイク」。clip 指定は LOADBG 不要の即応経路。"""
        parts = [f"PLAY {channel}-{layer}"]
        if clip is not None:
            parts.append(f'"{_escape(clip)}"')
            if loop:
                parts.append("LOOP")
        return await self.amcp(" ".join(parts))

    async def stop(self, channel: int, layer: int) -> AmcpResult:
        return await self.amcp(f"STOP {channel}-{layer}")

    async def clear(self, channel: int, layer: int) -> AmcpResult:
        return await self.amcp(f"CLEAR {channel}-{layer}")

    async def layer_foreground(self, channel: int, layer: int) -> dict | None:
        """INFO {ch}-{layer} を取得し foreground producer 状態を返す (出力 watchdog 用)。

        返り値は parse_foreground 参照。未接続/取得失敗/解析不能なら None
        (層が未占有=黒は None ではなく producer="empty" で返る)。
        """
        if not self._connected:
            return None
        try:
            result = await self.amcp(f"INFO {channel}-{layer}")
        except (OSError, ConnectionError, asyncio.IncompleteReadError):
            return None
        if not result.is_success or not result.body:
            return None
        return parse_foreground(result.body[0], layer)

    async def channel_layers(self, channel: int, layers: list[int]) -> dict[int, dict]:
        """INFO {ch} を 1 回取得し、各 layer の foreground producer/name を抽出 (#18 レイヤ状態)。

        INFO {ch}-{layer} はチャンネル全体の XML (<layer_NN> 群) を返すため、ここで層別に切る。
        返り値 {layer: {"producer": str, "name": str|None}}。未接続/失敗なら {}。
        """
        if not self._connected:
            return {}
        try:
            result = await self.amcp(f"INFO {channel}")
        except (OSError, ConnectionError, asyncio.IncompleteReadError):
            return {}
        if not result.is_success or not result.body:
            return {}
        return parse_layer_states(result.body[0], layers)

    async def health(self) -> str:
        if not self._connected:
            return "down"
        try:
            await self.version()
        except (AmcpError, OSError, ConnectionError, asyncio.IncompleteReadError):
            return "degraded"
        return "healthy"

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._connected = False


def _parse_code(header: str, command: str) -> int:
    parts = header.split(None, 1)
    if not parts:
        raise ValueError(f"empty AMCP header for command {command!r}")
    try:
        return int(parts[0])
    except ValueError as e:
        raise ValueError(f"AMCP header has no leading int: {header!r} (command={command!r})") from e


def _escape(s: str) -> str:
    """AMCP の clip 引数: " と \\ を escape。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


_FG_RE = re.compile(r"<foreground>(.*?)</foreground>", re.S)
_EMPTY_FG: dict = {"producer": "empty", "name": None, "time": None, "paused": False}


def parse_foreground(info_xml: str, layer: int) -> dict | None:
    """INFO <ch>-<layer> の XML から指定 layer の foreground producer 状態を抽出 (出力 watchdog 用)。

    返り値 {"producer": str, "name": str|None, "time": float|None, "paused": bool}。
    producer 不在は "empty" 扱い。channel XML と解釈できない入力のみ None (判定不能)。

    INFO {ch}-{layer} もチャンネル全体の XML (<layer_NN> 群) を返すので、要求した層の
    ブロックへ絞ってから解析する。絞らないと最小番号の占有レイヤ (本線が空なら L バー等)
    を本線と誤認する。占有されていない層は <layer_NN> ブロックごと出ないが、これは
    「取得失敗」ではなく黒なので "empty" を返す — casparcg 再起動直後がこの形で、
    None にすると出力 watchdog が黒を判定不能として見逃す (2026-08-21 の 35 分黒落ち)。
    """
    m = re.search(rf"<layer_{layer}>(.*?)</layer_{layer}>", info_xml, re.S)
    if m is None:
        return dict(_EMPTY_FG) if "<stage>" in info_xml else None
    fgm = _FG_RE.search(m.group(1))
    if fgm is None:
        return dict(_EMPTY_FG)
    fg = fgm.group(1)
    prod = re.search(r"<producer>([a-z_]+)</producer>", fg)
    name = re.search(r"<name>([^<]*)</name>", fg)
    tm = re.search(r"<time>([0-9.]+)</time>", fg)
    paused = re.search(r"<paused>([a-z]+)</paused>", fg)
    return {
        "producer": prod.group(1) if prod else "empty",
        "name": name.group(1) if name else None,
        "time": float(tm.group(1)) if tm else None,
        "paused": (paused.group(1) == "true") if paused else False,
    }


def parse_layer_states(channel_xml: str, layers: list[int]) -> dict[int, dict]:
    """INFO {ch} の channel XML から各 <layer_NN> の foreground producer/name を抽出 (#18)。

    占有レイヤのみ <layer_NN> ブロックが出る。ブロック内 <foreground> の producer/name を採る。
    ブロック無し = 空レイヤ。返り値 {layer: {"producer": str, "name": str|None}}。
    """
    out: dict[int, dict] = {}
    for n in layers:
        m = re.search(rf"<layer_{n}>(.*?)</layer_{n}>", channel_xml, re.S)
        if m is None:
            out[n] = {"producer": "empty", "name": None}
            continue
        fg = _FG_RE.search(m.group(1))
        block = fg.group(1) if fg else ""
        prod = re.search(r"<producer>([a-z_]+)</producer>", block)
        name = re.search(r"<name>([^<]*)</name>", block)
        out[n] = {
            "producer": prod.group(1) if prod else "empty",
            "name": name.group(1) if name else None,
        }
    return out


def output_is_bad(fg: dict | None, prev_name: str | None, prev_time: float | None) -> bool:
    """foreground が「黒(empty)」または「フリーズ(同一 clip で time 不変かつ非 pause)」なら True。

    fg=None (INFO 取得失敗) は判定不能として False。pause 中は意図的とみなし False。
    clip が変わった (loop 巻き戻し含む) 直後は time 比較せず False (誤検知防止)。
    """
    if fg is None:
        return False
    if fg["producer"] == "empty":
        return True
    if fg["paused"]:
        return False
    if fg["name"] != prev_name:
        return False
    t, pt = fg["time"], prev_time
    if t is None or pt is None:
        return False
    return abs(t - pt) < 1e-3
