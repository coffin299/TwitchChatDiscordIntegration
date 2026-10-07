"""config.yaml の servers セクションをコメントを保ったまま書き換える。"""

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


def _find_key(servers: dict[Any, Any], guild_id: int) -> Any:
    """マッピング形式の servers から guild_id に一致するキーを探す。"""
    for key in servers:
        # キーは int でも "123" のような文字列でも一致させる
        try:
            if int(key) == guild_id:
                return key
        except (TypeError, ValueError):
            continue
    return None


def _upsert_mapping(
    servers: CommentedMap,
    guild_id: int,
    channels: tuple[str, ...],
    voice_channel_id: int | None,
) -> None:
    """マッピング形式の servers にサーバー設定を追加・更新する。"""
    key = _find_key(servers, guild_id)
    current = servers.get(key) if key is not None else None
    # 既存の詳細設定（voice 等）があれば残したまま twitch だけ差し替える
    if isinstance(current, dict):
        current["twitch"] = _twitch_value(channels)
        # VC 指定があるときだけ上書き
        if voice_channel_id is not None:
            current["voice_channel_id"] = voice_channel_id
        return
    # VC 指定があれば詳細形式、無ければ「ID: チャンネル」の省略形で書く
    if voice_channel_id is not None:
        value: Any = CommentedMap(
            twitch=_twitch_value(channels), voice_channel_id=voice_channel_id
        )
    else:
        value = _twitch_value(channels)
    # 既存キーがあれば位置を保って置換、無ければ末尾に追加
    servers[key if key is not None else guild_id] = value


def _upsert_list(
    servers: list[Any],
    guild_id: int,
    channels: tuple[str, ...],
    voice_channel_id: int | None,
) -> None:
    """リスト形式の servers にサーバー設定を追加・更新する。"""
    for item in servers:
        # guild_id が一致する要素を更新
        if not isinstance(item, dict):
            continue
        if str(item.get("guild_id")) == str(guild_id):
            item["twitch"] = _twitch_value(channels)
            if voice_channel_id is not None:
                item["voice_channel_id"] = voice_channel_id
            return
    # 見つからなければ新しい要素を追加
    entry = CommentedMap(guild_id=guild_id, twitch=_twitch_value(channels))
    if voice_channel_id is not None:
        entry["voice_channel_id"] = voice_channel_id
    servers.append(entry)


def upsert_server(
    path: str | Path,
    guild_id: int,
    channels: tuple[str, ...],
    voice_channel_id: int | None = None,
) -> None:
    """config.yaml にサーバー設定を追加・更新する。

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

    servers = data.get("servers")
    # servers 未定義なら推奨のマッピング形式で新規作成
    if servers is None:
        servers = data["servers"] = CommentedMap()
    # 形式に応じて追加・更新
    if isinstance(servers, dict):
        _upsert_mapping(servers, guild_id, channels, voice_channel_id)
    elif isinstance(servers, list):
        _upsert_list(servers, guild_id, channels, voice_channel_id)
    else:
        raise ConfigError("servers はマッピングまたはリストである必要があります")

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
