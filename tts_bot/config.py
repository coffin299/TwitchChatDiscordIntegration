"""YAML 設定ファイルの読み込みと検証を担当するモジュール。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ${VAR} 形式の環境変数参照を検出する正規表現
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Twitch チャンネル URL からログイン名を取り出す正規表現
_TWITCH_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([^/?#\s]+)", re.IGNORECASE
)
# Twitch のログイン名として有効な文字列
_TWITCH_LOGIN_PATTERN = re.compile(r"[a-z0-9_]{1,25}")

# サポートする音声合成エンジンの種類
SUPPORTED_ENGINE_TYPES = ("voicevox", "coeiroink")

# 音声パラメータの既定値（defaults.voice で上書き可能）
_DEFAULT_VOICE: dict[str, Any] = {
    "engine": None,
    "speaker": 1,
    "speaker_uuid": None,
    "style_id": 0,
    "speed": 1.0,
    "pitch": 0.0,
    "intonation": 1.0,
    "volume": 1.0,
}

# 読み上げ設定の既定値（defaults.reading で上書き可能）
_DEFAULT_READING: dict[str, Any] = {
    "format": "{name}、{message}",
    "read_name": True,
    "max_length": 80,
    "truncate_suffix": "、以下略",
    "url_replacement": "URL省略",
    "strip_emotes": True,
    "ignore_users": [],
    "ignore_prefixes": ["!"],
    "dictionary": {},
    "max_queue": 20,
}


class ConfigError(Exception):
    """設定ファイルの内容が不正な場合に送出される例外。"""


@dataclass(frozen=True)
class EngineConfig:
    """音声合成エンジン 1 つ分の接続設定。"""

    name: str
    type: str
    url: str
    timeout: float = 30.0


@dataclass(frozen=True)
class VoiceConfig:
    """話者や話速など、音声合成に渡すパラメータ。"""

    engine: str
    speaker: int = 1
    speaker_uuid: str | None = None
    style_id: int = 0
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    volume: float = 1.0


@dataclass(frozen=True)
class ReadingConfig:
    """コメントをどう読み上げ用テキストに整形するかの設定。"""

    format: str
    read_name: bool
    max_length: int
    truncate_suffix: str
    url_replacement: str
    strip_emotes: bool
    ignore_users: frozenset[str]
    ignore_prefixes: tuple[str, ...]
    dictionary: dict[str, str]
    max_queue: int


@dataclass(frozen=True)
class UserConfig:
    """Discord ユーザー 1 人と、その人の Twitch チャンネルの対応設定。"""

    discord_user_id: int
    twitch_channels: tuple[str, ...]
    voice: VoiceConfig
    reading: ReadingConfig
    # Twitch 視聴者（ログイン名）ごとの声
    viewer_voices: dict[str, VoiceConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    """アプリケーション全体の設定。"""

    discord_token: str
    ffmpeg_path: str
    log_level: str
    engines: dict[str, EngineConfig]
    # Discord ユーザー ID → 設定
    users: dict[int, UserConfig]


def _expand_env(value: Any) -> Any:
    """文字列中の ${VAR} を環境変数で再帰的に置換する。"""
    # 文字列なら環境変数参照を展開（未定義は空文字）
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    # 辞書は値だけを再帰的に展開
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    # リストは各要素を再帰的に展開
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    # それ以外（数値・bool 等）はそのまま返す
    return value


def _as_dict(value: Any, where: str) -> dict[str, Any]:
    """値が辞書であることを検証し、None は空辞書として扱う。"""
    # 未指定は空辞書として扱う
    if value is None:
        return {}
    # 辞書以外は設定ミスとしてエラーにする
    if not isinstance(value, dict):
        raise ConfigError(f"{where} はマッピング形式で指定してください")
    return value


def normalize_channel(name: Any) -> str:
    """Twitch チャンネル名・URL を IRC で扱う小文字・# なしの形式に正規化する。

    ``https://www.twitch.tv/foo`` / ``twitch.tv/foo`` / ``#foo`` / ``foo``
    のいずれも ``foo`` になる。
    """
    text = str(name).strip()
    # URL 形式ならパスの先頭部分（ログイン名）だけを取り出す
    match = _TWITCH_URL_PATTERN.match(text)
    if match:
        text = match.group(1)
    # 先頭の # を除去し、小文字に統一
    channel = text.lstrip("#").lower()
    # Twitch のログイン名として不正な文字列はエラー
    if not _TWITCH_LOGIN_PATTERN.fullmatch(channel):
        raise ConfigError(f"Twitch チャンネル名として不正です: '{name}'")
    return channel


def _build_engines(raw: dict[str, Any]) -> dict[str, EngineConfig]:
    """engines セクションから EngineConfig の辞書を構築する。"""
    engines: dict[str, EngineConfig] = {}
    for name, body in raw.items():
        body = _as_dict(body, f"engines.{name}")
        # type 未指定時はエンジン名から推測する
        engine_type = str(body.get("type", name)).lower()
        # 未対応のエンジン種別はエラー
        if engine_type not in SUPPORTED_ENGINE_TYPES:
            raise ConfigError(
                f"engines.{name}.type は {SUPPORTED_ENGINE_TYPES} のいずれかです"
            )
        # URL は必須
        if not body.get("url"):
            raise ConfigError(f"engines.{name}.url が未指定です")
        engines[str(name)] = EngineConfig(
            name=str(name),
            type=engine_type,
            # 末尾スラッシュを除去してパス結合時の二重スラッシュを防ぐ
            url=str(body["url"]).rstrip("/"),
            timeout=float(body.get("timeout", 30.0)),
        )
    # エンジンが 1 つもなければ読み上げできない
    if not engines:
        raise ConfigError("engines に音声合成エンジンを 1 つ以上定義してください")
    return engines


def _build_voice(
    merged: dict[str, Any], engines: dict[str, EngineConfig], where: str
) -> VoiceConfig:
    """マージ済みの辞書から VoiceConfig を構築し、整合性を検証する。"""
    # エンジン未指定なら最初に定義されたエンジンを使う
    engine_name = merged.get("engine") or next(iter(engines))
    # 存在しないエンジン名はエラー
    if engine_name not in engines:
        raise ConfigError(f"{where}.engine '{engine_name}' は engines に未定義です")
    # COEIROINK は話者 UUID が必須
    is_coeiroink = engines[engine_name].type == "coeiroink"
    if is_coeiroink and not merged.get("speaker_uuid"):
        raise ConfigError(f"{where}: COEIROINK では speaker_uuid が必須です")
    return VoiceConfig(
        engine=engine_name,
        speaker=int(merged["speaker"]),
        speaker_uuid=merged.get("speaker_uuid"),
        style_id=int(merged["style_id"]),
        speed=float(merged["speed"]),
        pitch=float(merged["pitch"]),
        intonation=float(merged["intonation"]),
        volume=float(merged["volume"]),
    )


def _build_reading(merged: dict[str, Any]) -> ReadingConfig:
    """マージ済みの辞書から ReadingConfig を構築する。"""
    return ReadingConfig(
        format=str(merged["format"]),
        read_name=bool(merged["read_name"]),
        max_length=int(merged["max_length"]),
        truncate_suffix=str(merged["truncate_suffix"]),
        url_replacement=str(merged["url_replacement"]),
        strip_emotes=bool(merged["strip_emotes"]),
        # 無視ユーザーは大文字小文字を区別しないよう小文字化
        ignore_users=frozenset(str(u).lower() for u in merged["ignore_users"]),
        ignore_prefixes=tuple(str(p) for p in merged["ignore_prefixes"]),
        # 辞書のキー・値は文字列に統一
        dictionary={str(k): str(v) for k, v in merged["dictionary"].items()},
        max_queue=int(merged["max_queue"]),
    )


def _build_users(
    raw: Any,
    engines: dict[str, EngineConfig],
    default_voice: dict[str, Any],
    default_reading: dict[str, Any],
) -> dict[int, UserConfig]:
    """users セクション（``Discordユーザー ID: Twitch``）から設定を構築する。

    値は ``twitch_name`` / ``[name1, name2]`` の省略形、
    または ``{twitch: ..., voice: ..., reading: ..., viewer_voices: ...}``。
    """
    users: dict[int, UserConfig] = {}
    # 未定義（/tts_setting で後から登録する初期状態）は 0 人として扱う
    for raw_user_id, body in _as_dict(raw, "users").items():
        # Discord のユーザー ID は数値でなければならない
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"users のキー '{raw_user_id}' が Discord ユーザー ID ではありません"
            ) from exc
        where = f"users.{user_id}"
        # 値が文字列またはリストならチャンネル指定の省略記法とみなす
        if isinstance(body, (str, list)):
            body = {"twitch": body}
        body = _as_dict(body, where)

        # twitch は文字列 1 つ、またはリストで複数指定可能
        twitch_raw = body.get("twitch")
        if twitch_raw is None:
            raise ConfigError(f"{where}.twitch が未指定です")
        if not isinstance(twitch_raw, list):
            twitch_raw = [twitch_raw]
        # 正規化しつつ重複を除去（順序は維持）
        channels = tuple(
            dict.fromkeys(normalize_channel(c) for c in twitch_raw)
        )

        # 既定値 → ユーザー個別設定の順でマージ
        voice_dict = {
            **default_voice,
            **_as_dict(body.get("voice"), f"{where}.voice"),
        }
        reading_dict = {
            **default_reading,
            **_as_dict(body.get("reading"), f"{where}.reading"),
        }

        # Twitch 視聴者別の声はこのユーザーの声をベースに上書き
        viewer_voices: dict[str, VoiceConfig] = {}
        raw_viewers = _as_dict(
            body.get("viewer_voices"), f"{where}.viewer_voices"
        )
        for login, override in raw_viewers.items():
            viewer_where = f"{where}.viewer_voices.{login}"
            override = _as_dict(override, viewer_where)
            viewer_voices[str(login).lower()] = _build_voice(
                {**voice_dict, **override}, engines, viewer_where
            )

        users[user_id] = UserConfig(
            discord_user_id=user_id,
            twitch_channels=channels,
            voice=_build_voice(voice_dict, engines, f"{where}.voice"),
            reading=_build_reading(reading_dict),
            viewer_voices=viewer_voices,
        )
    return users


def load_config(path: str | Path, require_token: bool = True) -> AppConfig:
    """YAML 設定ファイルを読み込み、検証済みの AppConfig を返す。"""
    config_path = Path(path)
    # ファイルが無ければサンプルからのコピーを促す
    if not config_path.is_file():
        raise ConfigError(
            f"{config_path} が見つかりません。config.example.yaml をコピーしてください"
        )
    # UTF-8 で読み込む（YAML 構文エラーは設定エラーとして扱う）
    try:
        with config_path.open(encoding="utf-8") as fp:
            raw = yaml.safe_load(fp)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} の YAML 構文エラー: {exc}") from exc
    # 環境変数参照を展開
    data = _expand_env(_as_dict(raw, "ルート"))
    # 数値欄に文字列を書いた等の型エラーも設定エラーとして扱う
    try:
        return _parse_config(data, require_token)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise ConfigError(f"{config_path} の値が不正です: {exc!r}") from exc


def _parse_config(data: dict[str, Any], require_token: bool) -> AppConfig:
    """展開済みの設定辞書から AppConfig を構築する。"""
    discord_section = _as_dict(data.get("discord"), "discord")
    # トークンは YAML → 環境変数 DISCORD_TOKEN の順で探す
    token = discord_section.get("token") or os.environ.get("DISCORD_TOKEN", "")
    # コピペ時に混入しがちな空白・クォート・"Bot " 接頭辞を取り除く
    token = str(token).strip().strip("\"'").strip()
    token = token.removeprefix("Bot ").strip()
    if require_token and not token:
        raise ConfigError("discord.token（または環境変数 DISCORD_TOKEN）が未設定です")

    # 旧形式（サーバー ID 基準）のまま使われていたら移行を促す
    if data.get("servers"):
        raise ConfigError(
            "servers は廃止しました。users（Discordユーザー ID: Twitch）に"
            "書き換えてください"
        )
    engines = _build_engines(_as_dict(data.get("engines"), "engines"))
    defaults = _as_dict(data.get("defaults"), "defaults")
    # 組み込み既定値に defaults セクションを重ねる
    default_voice = {
        **_DEFAULT_VOICE,
        **_as_dict(defaults.get("voice"), "defaults.voice"),
    }
    default_reading = {
        **_DEFAULT_READING,
        **_as_dict(defaults.get("reading"), "defaults.reading"),
    }

    return AppConfig(
        discord_token=str(token),
        ffmpeg_path=str(data.get("ffmpeg_path", "ffmpeg")),
        log_level=str(data.get("log_level", "INFO")).upper(),
        engines=engines,
        users=_build_users(
            data.get("users"), engines, default_voice, default_reading
        ),
    )
