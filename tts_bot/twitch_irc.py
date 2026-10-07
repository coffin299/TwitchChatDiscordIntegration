"""Twitch チャットを匿名 IRC 接続で受信するモジュール。

読み取り専用のため OAuth トークンは不要（justinfan 匿名ログインを使用）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
from dataclasses import dataclass
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# Twitch IRC の TLS 接続先
TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697
# 再接続待機時間の上限（秒）
_MAX_BACKOFF = 60.0
# JOIN のレート制限（20 回 / 10 秒）を避けるための間隔（秒）
_JOIN_INTERVAL = 0.6
# 受信待ちのタイムアウト（Twitch の PING は約 5 分間隔）（秒）
_READ_TIMEOUT = 420.0
# IRC タグ値のエスケープ表
_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


@dataclass(frozen=True)
class ChatMessage:
    """受信したチャットメッセージ 1 件。"""

    channel: str
    login: str
    display_name: str
    text: str
    tags: dict[str, str]


def _unescape_tag(value: str) -> str:
    """IRCv3 のタグ値エスケープを元に戻す。"""
    result: list[str] = []
    chars = iter(value)
    for ch in chars:
        # バックスラッシュ以外はそのまま
        if ch != "\\":
            result.append(ch)
            continue
        # 次の 1 文字で置換内容を決める（末尾の孤立 \ は捨てる）
        nxt = next(chars, "")
        result.append(_TAG_ESCAPES.get(nxt, nxt))
    return "".join(result)


def parse_irc_line(line: str) -> tuple[dict[str, str], str, str, list[str]]:
    """IRC 行を (tags, prefix, command, params) に分解する。"""
    tags: dict[str, str] = {}
    prefix = ""
    # "@" で始まればタグ部
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
    # ":" で始まればプレフィックス部（送信者情報）
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    # " :" 以降は空白を含められる末尾パラメータ
    head, sep, trailing = line.partition(" :")
    parts = head.split()
    command = parts[0] if parts else ""
    params = parts[1:]
    # 末尾パラメータがあれば追加
    if sep:
        params.append(trailing)
    return tags, prefix, command, params


class TwitchChatClient:
    """複数チャンネルのチャットを 1 本の接続で受信し、コールバックへ渡す。"""

    def __init__(
        self,
        channels: Iterable[str],
        on_message: Callable[[ChatMessage], None],
    ) -> None:
        # 重複を除いたチャンネル一覧を保持
        self._channels = tuple(dict.fromkeys(channels))
        self._on_message = on_message

    async def run_forever(self) -> None:
        """切断されても指数バックオフで再接続し続ける。"""
        backoff = 1.0
        while True:
            try:
                await self._session()
                # 正常終了（RECONNECT 要求など）なら即再接続
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.IncompleteReadError) as exc:
                log.warning("Twitch IRC 切断: %s（%.0f 秒後に再接続）", exc, backoff)
                await asyncio.sleep(backoff)
                # 待機時間を倍にしつつ上限で頭打ち
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _session(self) -> None:
        """1 回分の接続セッション。切断されるまで受信を続ける。"""
        reader, writer = await asyncio.open_connection(
            TWITCH_IRC_HOST, TWITCH_IRC_PORT, ssl=ssl.create_default_context()
        )

        def send(raw: str) -> None:
            # IRC は CRLF 区切り
            writer.write(f"{raw}\r\n".encode("utf-8"))

        try:
            # タグ（表示名・エモート位置）とコマンド通知を要求
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            # 匿名ログイン（PASS は任意文字列、NICK は justinfan + 数字）
            send("PASS SCHMOOPIIE")
            send(f"NICK justinfan{random.randint(10000, 99999)}")
            await writer.drain()
            # 受信を先に開始し、JOIN は並行して送る
            join_task = asyncio.create_task(self._join_all(send, writer))
            try:
                await self._read_loop(reader, send, writer)
            finally:
                join_task.cancel()
        finally:
            writer.close()

    async def _join_all(
        self, send: Callable[[str], None], writer: asyncio.StreamWriter
    ) -> None:
        """全チャンネルへレート制限を守りつつ JOIN する。"""
        for channel in self._channels:
            send(f"JOIN #{channel}")
            await writer.drain()
            log.info("Twitch #%s に接続しました", channel)
            await asyncio.sleep(_JOIN_INTERVAL)

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        send: Callable[[str], None],
        writer: asyncio.StreamWriter,
    ) -> None:
        """切断または RECONNECT まで IRC 行を読み続ける。"""
        while True:
            # 無通信の半死接続を検出するため PING 間隔より長めのタイムアウトを設ける
            raw = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
            # 空バイトは接続終了
            if not raw:
                raise ConnectionError("サーバーが接続を閉じました")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tags, prefix, command, params = parse_irc_line(line)
            # 生存確認には PONG で応答
            if command == "PING":
                send(f"PONG :{params[-1] if params else 'tmi.twitch.tv'}")
                await writer.drain()
            # サーバーメンテナンス等による再接続要求
            elif command == "RECONNECT":
                log.info("Twitch から再接続要求を受信しました")
                return
            # 通常のチャットメッセージ
            elif command == "PRIVMSG" and len(params) >= 2:
                self._dispatch(tags, prefix, params)

    def _dispatch(
        self, tags: dict[str, str], prefix: str, params: list[str]
    ) -> None:
        """PRIVMSG を ChatMessage に変換してコールバックを呼ぶ。"""
        # プレフィックス "login!login@login.tmi.twitch.tv" からログイン名を取得
        login = prefix.split("!", 1)[0].lower()
        text = params[1]
        # /me 発言（ACTION）は CTCP の囲みを外す
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            text = text[len("\x01ACTION "):-1]
        message = ChatMessage(
            channel=params[0].lstrip("#").lower(),
            login=login,
            # 表示名が無ければログイン名で代用
            display_name=tags.get("display-name") or login,
            text=text,
            tags=tags,
        )
        # コールバック内の例外で受信ループが止まらないよう保護
        try:
            self._on_message(message)
        except Exception:  # noqa: BLE001
            log.exception("チャット処理中にエラーが発生しました")
