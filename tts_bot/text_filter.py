"""Twitch チャットを読み上げ用テキストへ整形するモジュール。"""

from __future__ import annotations

import re

from .config import ReadingConfig
from .twitch_irc import ChatMessage

# URL を検出する正規表現
_URL_PATTERN = re.compile(r"https?://\S+")
# 連続する空白を 1 つにまとめるための正規表現
_SPACES_PATTERN = re.compile(r"\s+")


def strip_emotes(text: str, emotes_tag: str) -> str:
    """IRC の emotes タグに基づき、メッセージからエモート部分を除去する。

    emotes タグは ``25:0-4,12-16/1902:6-10`` のような形式で、
    位置はコードポイント単位（Python の str インデックスと一致）。
    """
    # タグが空ならエモートは含まれていない
    if not emotes_tag:
        return text
    removed: set[int] = set()
    # エモート ID ごとに "/" で区切られている
    for emote in emotes_tag.split("/"):
        # "ID:範囲,範囲" の範囲部分だけを取り出す
        _, _, ranges = emote.partition(":")
        for span in ranges.split(","):
            start, _, end = span.partition("-")
            # 数値として解釈できない範囲は無視する
            if not (start.isdigit() and end.isdigit()):
                continue
            # 終端を含む範囲を削除対象に追加
            removed.update(range(int(start), int(end) + 1))
    # 削除対象以外の文字だけを連結
    return "".join(ch for i, ch in enumerate(text) if i not in removed)


def apply_dictionary(
    text: str, dictionary: dict[str, str], *, ignore_case: bool = False
) -> str:
    """読み替え辞書を適用する（長い語から順に置換して部分一致の衝突を防ぐ）。"""
    for word in sorted(dictionary, key=len, reverse=True):
        # 空キーはあらゆる位置に一致してしまう
        if not word:
            continue
        reading = dictionary[word]
        if not ignore_case:
            text = text.replace(word, reading)
            continue
        # 読みの中の \\ を正規表現の後方参照として解釈しない
        text = re.sub(
            re.escape(word),
            lambda _match, repl=reading: repl,
            text,
            flags=re.IGNORECASE,
        )
    return text


def lookup_reading(names: tuple[str, ...], dictionary: dict[str, str]) -> str | None:
    """ログイン名または表示名が辞書と一致したら、その読みを返す。

    一致は語句全体（大文字小文字は無視）。部分一致では名前を変えない。
    """
    if not dictionary:
        return None
    folded: dict[str, str] = {}
    for key, value in dictionary.items():
        if key:
            folded.setdefault(key.casefold(), value)
    for name in names:
        if name in dictionary:
            return dictionary[name]
        hit = folded.get(name.casefold())
        if hit is not None:
            return hit
    return None


def build_speech_text(
    message: ChatMessage,
    reading: ReadingConfig,
    viewer_dictionary: dict[str, str] | None = None,
) -> str | None:
    """チャットメッセージを読み上げテキストへ変換する。読まない場合は None。"""
    # 無視ユーザー（Bot 等）は読まない
    if message.login in reading.ignore_users:
        return None
    # コマンド等の特定プレフィックスで始まるものは読まない
    prefixes = reading.ignore_prefixes
    if prefixes and message.text.startswith(prefixes):
        return None

    body = message.text
    # エモートを除去（位置ずれを防ぐため他の加工より先に行う）
    if reading.strip_emotes:
        body = strip_emotes(body, message.tags.get("emotes", ""))
    # URL を置換
    body = _URL_PATTERN.sub(reading.url_replacement, body)
    # dictionary.yaml を先に適用し、同じ語はこちらを優先する
    viewers = viewer_dictionary or {}
    body = apply_dictionary(body, viewers, ignore_case=True)
    # config.yaml の読み替え辞書を適用
    body = apply_dictionary(body, reading.dictionary)
    # 空白を整理
    body = _SPACES_PATTERN.sub(" ", body).strip()
    # 本文が空（エモートのみ等）なら読まない
    if not body:
        return None
    # 長すぎる本文は切り詰めて省略記号を付ける
    if reading.max_length > 0 and len(body) > reading.max_length:
        body = body[: reading.max_length] + reading.truncate_suffix

    # 名前を読まない設定なら本文だけ返す
    if not reading.read_name:
        return body
    # ログイン名か表示名が登録されていれば、名前全体をその読みにする
    name = lookup_reading((message.display_name, message.login), viewers)
    if name is None:
        # 未登録の表示名には config.yaml の辞書だけを適用する
        name = apply_dictionary(message.display_name, reading.dictionary)
    # {channel} で配信チャンネル名も読める（複数配信者が同じ VC にいる場合向け）
    return reading.format.format(
        name=name, message=body, channel=message.channel
    )
