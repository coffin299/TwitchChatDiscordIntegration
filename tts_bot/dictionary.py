"""dictionary.yaml（配信者ごとの読み替え）の読み書き。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .config import ConfigError

# 1 人あたりの登録上限（コマンドの連打でファイルが膨らまないようにする）
MAX_DICTIONARY_ENTRIES = 300
# 語句・読みの最大文字数
MAX_DICTIONARY_TEXT = 100


def validate_entry(word: str, reading: str) -> tuple[str, str]:
    """登録する語句と読みを検証して、前後の空白を除いた組を返す。"""
    word = word.strip()
    reading = reading.strip()
    if not word or not reading:
        raise ConfigError("語句と読みの両方を入力してください")
    if "\n" in word or "\r" in word or "\n" in reading or "\r" in reading:
        raise ConfigError("語句と読みに改行は使えません")
    if len(word) > MAX_DICTIONARY_TEXT or len(reading) > MAX_DICTIONARY_TEXT:
        raise ConfigError(
            f"語句と読みは {MAX_DICTIONARY_TEXT} 文字までです"
        )
    return word, reading


def load_dictionary(path: str | Path) -> dict[int, dict[str, str]]:
    """dictionary.yaml を読む。ファイルが無ければ空。

    形式は ``users: {DiscordユーザーID: {語句: 読み}}``。
    """
    dictionary_path = Path(path)
    if not dictionary_path.is_file() or dictionary_path.stat().st_size == 0:
        return {}
    yaml = _round_trip_yaml()
    try:
        with dictionary_path.open(encoding="utf-8") as fp:
            raw = yaml.load(fp)
    except YAMLError as exc:
        raise ConfigError(
            f"{dictionary_path} の YAML 構文エラー: {exc}"
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{dictionary_path} のルートがマッピングではありません"
        )
    users = raw.get("users")
    if users is None:
        return {}
    if not isinstance(users, dict):
        raise ConfigError(f"{dictionary_path} の users はマッピングで指定してください")

    loaded: dict[int, dict[str, str]] = {}
    for raw_user_id, body in users.items():
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"dictionary のキー '{raw_user_id}' が Discord ユーザー ID ではありません"
            ) from exc
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ConfigError(
                f"dictionary.users.{user_id} はマッピングで指定してください"
            )
        entries: dict[str, str] = {}
        for key, value in body.items():
            # true / false は読みの語句として扱わない（YAML の真偽値）
            if isinstance(key, bool) or isinstance(value, bool):
                raise ConfigError(
                    f"dictionary.users.{user_id} の語句と読みは文字列で指定してください"
                )
            if isinstance(key, (dict, list)) or isinstance(value, (dict, list)):
                raise ConfigError(
                    f"dictionary.users.{user_id} の語句と読みは文字列で指定してください"
                )
            word = str(key).strip()
            reading = str(value).strip()
            if not word or not reading:
                raise ConfigError(
                    f"dictionary.users.{user_id} に空の語句または読みがあります"
                )
            entries[word] = reading
        if entries:
            loaded[user_id] = entries
    return loaded


def upsert_entry(
    path: str | Path, user_id: int, word: str, reading: str
) -> Literal["added", "updated"]:
    """語句を追加または更新し、追加なら added、上書きなら updated を返す。"""
    word, reading = validate_entry(word, reading)
    dictionary_path = Path(path)
    yaml, data = _load_document(dictionary_path)
    users = _users_map(data)
    key = _find_user_key(users, user_id)
    current = users.get(key) if key is not None else None
    if current is None:
        current = CommentedMap()
        users[key if key is not None else user_id] = current
    elif not isinstance(current, dict):
        raise ConfigError(
            f"dictionary.users.{user_id} はマッピングである必要があります"
        )

    existing = _find_word(current, word)
    status: Literal["added", "updated"] = "updated" if existing is not None else "added"
    if existing is None and len(current) >= MAX_DICTIONARY_ENTRIES:
        raise ConfigError(
            f"辞書は 1 人あたり {MAX_DICTIONARY_ENTRIES} 件までです"
        )
    # 大文字小文字だけ違う既存キーは、新しい表記に揃える
    if existing is not None and str(existing) != word:
        del current[existing]
        existing = None
    target = existing if existing is not None else _quoted(word)
    current[target] = _quoted(reading)
    _save_document(dictionary_path, yaml, data)
    return status


def remove_entry(path: str | Path, user_id: int, word: str) -> bool:
    """語句を削除する。削除できたら True、無ければ False。"""
    word = word.strip()
    if not word:
        raise ConfigError("削除する語句を入力してください")
    dictionary_path = Path(path)
    # ファイルが無ければ削除対象もない
    if not dictionary_path.is_file():
        return False
    yaml, data = _load_document(dictionary_path)
    users = data.get("users")
    if not isinstance(users, dict):
        return False
    key = _find_user_key(users, user_id)
    if key is None or not isinstance(users.get(key), dict):
        return False
    entries = users[key]
    existing = _find_word(entries, word)
    if existing is None:
        return False
    del entries[existing]
    # 空になったユーザーは残さない
    if not entries:
        del users[key]
    _save_document(dictionary_path, yaml, data)
    return True


def _quoted(text: str) -> DoubleQuotedScalarString:
    """true や 8888 が YAML の別の型にならないよう、常に文字列として書く。"""
    return DoubleQuotedScalarString(text)


def _round_trip_yaml() -> YAML:
    """コメントとインデントを維持する YAML ハンドラを作る。"""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _blank_document() -> CommentedMap:
    """新規 dictionary.yaml のひな型。"""
    data = CommentedMap()
    data.yaml_set_start_comment(
        "よく来る人・語句の読み替え（config.yaml と同じフォルダ）\n"
        "/dictionary add で更新する。users のキーは Discord ユーザー ID。\n"
        "各項目は「Twitchのログイン名または表示名: 読み上げるよみ」。"
    )
    data["users"] = CommentedMap()
    return data


def _load_document(path: Path) -> tuple[YAML, dict[Any, Any]]:
    """コメントを保ったまま読む。無ければひな型を返す。"""
    yaml = _round_trip_yaml()
    if not path.is_file() or path.stat().st_size == 0:
        return yaml, _blank_document()
    try:
        with path.open(encoding="utf-8") as fp:
            data = yaml.load(fp)
    except YAMLError as exc:
        raise ConfigError(f"{path} の YAML 構文エラー: {exc}") from exc
    if data is None:
        return yaml, _blank_document()
    if not isinstance(data, dict):
        raise ConfigError(f"{path} のルートがマッピングではありません")
    return yaml, data


def _users_map(data: dict[Any, Any]) -> dict[Any, Any]:
    """users セクションを用意して返す。"""
    users = data.get("users")
    if users is None:
        users = data["users"] = CommentedMap()
    if not isinstance(users, dict):
        raise ConfigError("dictionary の users はマッピングである必要があります")
    return users


def _find_user_key(users: dict[Any, Any], user_id: int) -> Any:
    """users から user_id に一致するキーを探す。"""
    for key in users:
        try:
            if int(key) == user_id:
                return key
        except (TypeError, ValueError):
            continue
    return None


def _find_word(entries: dict[Any, Any], word: str) -> Any:
    """大文字小文字を無視して語句キーを探す。"""
    folded = word.casefold()
    for key in entries:
        if str(key).casefold() == folded:
            return key
    return None


def _save_document(path: Path, yaml: YAML, data: dict[Any, Any]) -> None:
    """検証してから本体と置き換える。失敗時は元ファイルを残す。"""
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fp:
            yaml.dump(data, fp)
        load_dictionary(tmp_path)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
