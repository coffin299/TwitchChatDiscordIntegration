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


def apply_dictionary(text: str, dictionary: dict[str, str]) -> str:
    """読み替え辞書を適用する（長い語から順に置換して部分一致の衝突を防ぐ）。"""
    for word in sorted(dictionary, key=len, reverse=True):
        text = text.replace(word, dictionary[word])
    return text


def build_speech_text(
    message: ChatMessage, reading: ReadingConfig
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
    # 読み替え辞書を適用
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
    # 表示名にも辞書を適用してから書式に埋め込む
    name = apply_dictionary(message.display_name, reading.dictionary)
    return reading.format.format(name=name, message=body)
