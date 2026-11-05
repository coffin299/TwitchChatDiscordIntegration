"""読み上げ直前に、英語表記をカタカナへ寄せる。

単語らしい英語は VOICEVOX の kanalizer で読む（エンジン本体と同じ系統）。
GG や OK のように略語・全て大文字は、エービーシーと一文字ずつ読む。
日本語・記号・「w」の連続（笑い）はそのまま残す。
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections.abc import Callable

log = logging.getLogger(__name__)

# 半角・全角の英字と、語中のアポストロフィ（I'm / don't）
_LATIN = r"A-Za-z\uFF21-\uFF3A\uFF41-\uFF5A"
_WORD = re.compile(rf"[{_LATIN}]+(?:['’][{_LATIN}]+)*")
# キャメルケース分割（VoiceVox → Voice / Vox）。VOICEVOX Engine と同じ方針
_CAMEL = re.compile(r"[a-zA-Z][a-z]*")
# チャットで略語として文字読みしたい語（小文字でも kanalizer に渡さない）
_LETTER_READING = frozenset({"ok", "url", "imo", "pls", "plz", "ty"})

# OpenJTalk のアルファベット→カタカナ対応（VOICEVOX Engine と同じ表）
_OJT_ALPHABET = {
    "A": "エー",
    "B": "ビー",
    "C": "シー",
    "D": "ディー",
    "E": "イー",
    "F": "エフ",
    "G": "ジー",
    "H": "エイチ",
    "I": "アイ",
    "J": "ジェー",
    "K": "ケー",
    "L": "エル",
    "M": "エム",
    "N": "エヌ",
    "O": "オー",
    "P": "ピー",
    "Q": "キュー",
    "R": "アール",
    "S": "エス",
    "T": "ティー",
    "U": "ユー",
    "V": "ブイ",
    "W": "ダブリュー",
    "X": "エックス",
    "Y": "ワイ",
    "Z": "ズィー",
}

_Convert = Callable[[str], str]
_converter: _Convert | None = None
_unavailable = False
_lock = threading.Lock()


def _char_wise_katakana(alphabets: str) -> str:
    """アルファベット列を文字ごとのカタカナ読みにする。"""
    return "".join(_OJT_ALPHABET[ch.upper()] for ch in alphabets)


def _should_use_kanalizer(word: str) -> bool:
    """VOICEVOX Engine と同じく、単語読みにするか文字読みにするか判定する。"""
    # 1 文字は変換しない（OpenJTalk 側の文字読みに寄せる）
    if len(word) == 1:
        return False
    # 全て大文字は略語扱い（VOICE / GG など）
    if word == word.upper():
        return False
    # チャット定番の略語は小文字でも文字読み
    if word.lower() in _LETTER_READING:
        return False
    return True


def _convert_alphabet(alphabets: str, convert: _Convert) -> str:
    """半角英字だけの文字列をカタカナへ変換する。"""
    kana = ""
    # キャメルケース単位で区切り、単語ごとに kanalizer / 文字読みを選ぶ
    for word in _CAMEL.findall(alphabets):
        if _should_use_kanalizer(word):
            kana += convert(word.lower())
        else:
            kana += _char_wise_katakana(word)
    return kana


def _reading(raw: str, convert: _Convert) -> str:
    """トークン 1 つを読み上げ用の文字列にする。失敗したら元の表記を残す。"""
    normalized = unicodedata.normalize("NFKC", raw)
    # アポストロフィはモデル入力から外し、I'm → Im のように連結する
    folded = normalized.replace("’", "'").replace("'", "")
    letters = "".join(ch for ch in folded if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    if not letters:
        return raw
    # www は笑いなので読まない（ウウ などにはしない）
    if set(letters.lower()) == {"w"}:
        return "w" * len(letters)
    try:
        return _convert_alphabet(letters, convert) or raw
    except Exception:
        log.debug("カタカナ化をスキップ: %s", raw, exc_info=True)
        return raw


def _load() -> _Convert | None:
    """kanalizer を遅延ロードする。未インストールや初期化失敗時は None。"""
    global _converter, _unavailable
    if _converter is not None or _unavailable:
        return _converter
    with _lock:
        if _converter is not None or _unavailable:
            return _converter
        try:
            import kanalizer

            # VOICEVOX Engine と同じく kanalizer.convert を使う
            _converter = kanalizer.convert
        except Exception:
            _unavailable = True
            log.warning(
                "英語のカタカナ変換を初期化できません。原文のまま読みます",
                exc_info=True,
            )
    return _converter


def warm_english_kana() -> None:
    """起動時にモデルを読み、最初のコメントで詰まらないようにする。"""
    convert = _load()
    # 初回推論の遅延を起動時に済ませる
    if convert is not None:
        with _lock:
            convert("warm")


def english_to_kana(text: str) -> str:
    """英語を含むテキストを、音声合成へ渡す直前の形にする。"""
    if not text or _WORD.search(text) is None:
        return text
    convert = _load()
    if convert is None:
        return text
    try:
        return _WORD.sub(lambda match: _reading(match.group(0), convert), text)
    except Exception:
        log.warning("英語のカタカナ変換に失敗したため原文を読みます", exc_info=True)
        return text
