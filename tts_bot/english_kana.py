"""読み上げ直前に、英語表記をカタカナへ寄せる。

単語らしい英語は e2k の文字→カタカナモデルで読む。
GG や OK のように単語として読めないものは、エービーシーと一文字ずつ読む。
日本語・記号・「w」の連続（笑い）はそのまま残す。
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import threading
import unicodedata
from typing import Protocol

log = logging.getLogger(__name__)

# 半角・全角の英字と、語中のアポストロフィ（I'm / don't）
_LATIN = r"A-Za-z\uFF21-\uFF3A\uFF41-\uFF5A"
_WORD = re.compile(rf"[{_LATIN}]+(?:['’][{_LATIN}]+)*")
# n-gram が英単語だと誤認し、モデルの読みが崩れる略語は文字読みにする
_LETTER_READING = frozenset({"ok", "url", "imo", "pls", "plz", "ty"})
# 辞書に無くても、モデルの読みの方が自然な短い語
_FORCE_WORD = frozenset({"yo", "yep"})


class _KanaModel(Protocol):
    def __call__(self, word: str) -> str: ...


class _WordModel(Protocol):
    def __call__(self, word: str) -> bool: ...

    def as_is(self, word: str) -> str: ...


class EnglishToKana:
    """英語トークンだけをカタカナに置き換える。"""

    def __init__(self, c2k: _KanaModel, ngram: _WordModel) -> None:
        self._c2k = c2k
        self._ngram = ngram

    def convert(self, text: str) -> str:
        """文中の英語だけをカタカナにする。"""
        return _WORD.sub(lambda match: self._reading(match.group(0)), text)

    def _is_word(self, word: str) -> bool:
        """英単語らしいか。ライブラリが未知語で標準出力へ警告を出すため握りつぶす。"""
        with contextlib.redirect_stdout(io.StringIO()):
            return bool(self._ngram(word))

    def _reading(self, raw: str) -> str:
        """トークン 1 つを読み上げ用の文字列にする。失敗したら元の表記を残す。"""
        normalized = unicodedata.normalize("NFKC", raw)
        # モデルはアポストロフィを落とすと don't → ドント のように安定する
        folded = normalized.replace("’", "'").replace("'", "").lower()
        folded = "".join(ch for ch in folded if "a" <= ch <= "z")
        if not folded:
            return raw
        # www は笑いなので読まない（ウウ などにはしない）
        if set(folded) == {"w"}:
            return "w" * len(folded)
        try:
            if folded in _FORCE_WORD:
                return self._c2k(folded) or raw
            if folded in _LETTER_READING or not self._is_word(folded):
                # I'm は文字読みだとアイエムになるので、語として変換する
                if ("'" in normalized or "’" in normalized) and (
                    folded not in _LETTER_READING
                ):
                    return self._c2k(normalized.lower()) or raw
                return self._ngram.as_is(folded) or raw
            return self._c2k(folded) or raw
        except Exception:
            log.debug("カタカナ化をスキップ: %s", raw, exc_info=True)
            return raw


_converter: EnglishToKana | None = None
_unavailable = False
_lock = threading.Lock()


def _load() -> EnglishToKana | None:
    """e2k を遅延ロードする。未インストールや初期化失敗時は None。"""
    global _converter, _unavailable
    if _converter is not None or _unavailable:
        return _converter
    with _lock:
        if _converter is not None or _unavailable:
            return _converter
        try:
            from e2k import C2K, NGram

            # 長いニックネームでも読みが途中で切れないよう、出力長の上限を広げる
            _converter = EnglishToKana(C2K(max_len=64), NGram())
        except Exception:
            _unavailable = True
            log.warning(
                "英語のカタカナ変換を初期化できません。原文のまま読みます",
                exc_info=True,
            )
    return _converter


def warm_english_kana() -> None:
    """起動時にモデルを読み、最初のコメントで詰まらないようにする。"""
    _load()


def english_to_kana(text: str) -> str:
    """英語を含むテキストを、音声合成へ渡す直前の形にする。"""
    if not text or _WORD.search(text) is None:
        return text
    converter = _load()
    if converter is None:
        return text
    try:
        return converter.convert(text)
    except Exception:
        log.warning("英語のカタカナ変換に失敗したため原文を読みます", exc_info=True)
        return text
