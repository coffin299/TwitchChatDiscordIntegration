"""config.yaml の users セクションをコメントを保ったまま書き換える。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap

from .config import ConfigError, load_config


def _round_trip_yaml() -> YAML:
    """コメント・クォート・インデントを維持する YAML ハンドラを作る。"""
    yaml = YAML()
    # 元ファイルのクォート表記を維持
    yaml.preserve_quotes = True
    # config.example.yaml と同じインデント幅に揃える
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _twitch_value(channels: tuple[str, ...]) -> Any:
    """チャンネルが 1 つなら文字列、複数ならリストとして書く。"""
    return channels[0] if len(channels) == 1 else list(channels)


def _find_key(users: dict[Any, Any], user_id: int) -> Any:
    """users から user_id に一致するキーを探す。"""
    for key in users:
        # キーは int でも "123" のような文字列でも一致させる
        try:
            if int(key) == user_id:
                return key
        except (TypeError, ValueError):
            continue
    return None


def upsert_user(
    path: str | Path, user_id: int, channels: tuple[str, ...]
) -> None:
    """config.yaml の users に Discord ユーザー ID: Twitch を追加・更新する。

    書き込み前に一時ファイルで検証し、問題なければ置き換える（原子的更新）。
    検証に失敗した場合は元ファイルを変更せず ConfigError を送出する。
    """
    config_path = Path(path)
    yaml = _round_trip_yaml()
    # 元ファイルをコメント付きで読み込む（構文エラーは設定エラーとして扱う）
    try:
        with config_path.open(encoding="utf-8") as fp:
            data = yaml.load(fp)
    except YAMLError as exc:
        raise ConfigError(f"{config_path} の YAML 構文エラー: {exc}") from exc
    # 空ファイルやルートが辞書でない場合は編集できない
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} のルートがマッピングではありません")

    users = data.get("users")
    # users 未定義なら新規作成
    if users is None:
        users = data["users"] = CommentedMap()
    if not isinstance(users, dict):
        raise ConfigError("users はマッピングである必要があります")

    key = _find_key(users, user_id)
    current = users.get(key) if key is not None else None
    # 既存の詳細設定（voice 等）があれば残したまま twitch だけ差し替える
    if isinstance(current, dict):
        current["twitch"] = _twitch_value(channels)
    else:
        # 既存キーがあれば位置を保って置換、無ければ末尾に追加
        users[key if key is not None else user_id] = _twitch_value(channels)

    # 一時ファイルに書き出して、設定として正しいか検証
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fp:
            yaml.dump(data, fp)
        load_config(tmp_path)
        # 検証 OK なら本体と置き換える
        os.replace(tmp_path, config_path)
    finally:
        # 検証失敗時に一時ファイルを残さない
        tmp_path.unlink(missing_ok=True)
