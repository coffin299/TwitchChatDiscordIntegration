"""登録チャンネルが配信中かどうかだけを調べる。

チャット受信は匿名 IRC のままにして、配信の開始・終了の判定だけ
Twitch の公開 Web クライアント向け API を使う（アプリのトークンは不要）。
"""

from __future__ import annotations

from typing import Any

import aiohttp

# twitch.tv が配信状態の問い合わせに使っている公開 Client-ID
_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_URL = "https://gql.twitch.tv/gql"
_QUERY = """
query($logins: [String!]) {
  users(logins: $logins) {
    login
    stream { type }
  }
}
"""
# 1 リクエストに載せるチャンネル数
_CHUNK = 50
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class TwitchLiveError(Exception):
    """配信状態を取得できなかった場合の例外。"""


def parse_live_logins(payload: Any) -> set[str]:
    """GQL 応答から配信中（type が live）のログイン名を取り出す。"""
    if not isinstance(payload, dict):
        raise TwitchLiveError("配信状態の応答が不正です")
    # data が無いエラー応答は、空のオフライン扱いにしない（直前の状態を維持する）
    data = payload.get("data")
    if payload.get("errors") and not isinstance(data, dict):
        raise TwitchLiveError("配信状態の取得に失敗しました")
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise TwitchLiveError("配信状態の応答が不正です")
    live: set[str] = set()
    for user in data["users"]:
        # 存在しないログイン名は null で返る
        if not isinstance(user, dict):
            continue
        login = user.get("login")
        stream = user.get("stream")
        if not isinstance(login, str) or not isinstance(stream, dict):
            continue
        # 再放送は配信開始とみなさない
        if stream.get("type", "live") != "live":
            continue
        live.add(login.lower())
    return live


class TwitchLiveChecker:
    """複数チャンネルの配信中一覧をまとめて取得する。"""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def live_logins(self, logins: list[str]) -> set[str]:
        """配信中のログイン名（小文字）を返す。失敗時は TwitchLiveError。"""
        found: set[str] = set()
        for start in range(0, len(logins), _CHUNK):
            found |= await self._fetch_chunk(logins[start : start + _CHUNK])
        return found

    async def _fetch_chunk(self, logins: list[str]) -> set[str]:
        payload = {"query": _QUERY, "variables": {"logins": logins}}
        headers = {
            "Client-ID": _CLIENT_ID,
            "User-Agent": "TwitchChatDiscordIntegration",
        }
        try:
            async with self._session.post(
                _URL, json=payload, headers=headers, timeout=_TIMEOUT
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise TwitchLiveError(f"HTTP {resp.status}")
        except TwitchLiveError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise TwitchLiveError(str(exc)) from exc
        return parse_live_logins(body)
