"""VOICEVOX / COEIROINK の HTTP API を呼び出して WAV を生成するモジュール。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import aiohttp

from .config import EngineConfig, VoiceConfig


class TTSError(Exception):
    """音声合成エンジンとの通信に失敗した場合の例外。"""


class TTSEngine(ABC):
    """音声合成エンジンの共通インターフェース。"""

    def __init__(
        self, config: EngineConfig, session: aiohttp.ClientSession
    ) -> None:
        self.config = config
        self._session = session
        # エンジンごとのタイムアウトをリクエスト単位で適用する
        self._timeout = aiohttp.ClientTimeout(total=config.timeout)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """API を呼び出し、JSON なら dict/list、それ以外は bytes を返す。"""
        url = f"{self.config.url}{path}"
        try:
            async with self._session.request(
                method, url, timeout=self._timeout, **kwargs
            ) as resp:
                # エラー応答は本文の先頭を含めて例外化
                if resp.status >= 400:
                    detail = (await resp.text())[:200]
                    raise TTSError(f"{url} -> HTTP {resp.status}: {detail}")
                # JSON 応答ならパースして返す
                if resp.content_type == "application/json":
                    return await resp.json()
                # 音声などバイナリはそのまま返す
                return await resp.read()
        except aiohttp.ClientError as exc:
            raise TTSError(f"{url} に接続できません: {exc}") from exc
        except TimeoutError as exc:
            raise TTSError(f"{url} がタイムアウトしました") from exc

    @abstractmethod
    async def synthesize(self, text: str, voice: VoiceConfig) -> bytes:
        """テキストを WAV バイト列に変換する。"""

    @abstractmethod
    async def list_speakers(self) -> list[str]:
        """設定ファイルに書くための話者一覧（人間向け文字列）を返す。"""


class VoicevoxEngine(TTSEngine):
    """VOICEVOX 互換 API（VOICEVOX / COEIROINK v1 / SHAREVOX など）。"""

    async def synthesize(self, text: str, voice: VoiceConfig) -> bytes:
        params = {"speaker": voice.speaker}
        # 1. テキストからアクセント句などの合成クエリを生成
        query = await self._request(
            "POST", "/audio_query", params={**params, "text": text}
        )
        # 2. 話速・音高・抑揚・音量を設定値で上書き
        query["speedScale"] = voice.speed
        query["pitchScale"] = voice.pitch
        query["intonationScale"] = voice.intonation
        query["volumeScale"] = voice.volume
        # 3. クエリから WAV を合成
        return await self._request(
            "POST", "/synthesis", params=params, json=query
        )

    async def list_speakers(self) -> list[str]:
        speakers = await self._request("GET", "/speakers")
        # スタイルごとに "話者名（スタイル名）: speaker: ID" の形で列挙
        return [
            f"{sp['name']}（{st['name']}）  speaker: {st['id']}"
            for sp in speakers
            for st in sp.get("styles", [])
        ]


class CoeiroinkEngine(TTSEngine):
    """COEIROINK v2 の独自 API。"""

    async def synthesize(self, text: str, voice: VoiceConfig) -> bytes:
        # 1. テキストから韻律（アクセント）情報を推定
        prosody = await self._request(
            "POST", "/v1/estimate_prosody", json={"text": text}
        )
        # 2. 韻律情報と話者・各種パラメータを指定して WAV を合成
        body = {
            "speakerUuid": voice.speaker_uuid,
            "styleId": voice.style_id,
            "text": text,
            "prosodyDetail": prosody.get("detail", []),
            "speedScale": voice.speed,
            "volumeScale": voice.volume,
            "pitchScale": voice.pitch,
            "intonationScale": voice.intonation,
            "prePhonemeLength": 0.1,
            "postPhonemeLength": 0.1,
            "outputSamplingRate": 24000,
        }
        return await self._request("POST", "/v1/synthesis", json=body)

    async def list_speakers(self) -> list[str]:
        speakers = await self._request("GET", "/v1/speakers")
        # スタイルごとに UUID と styleId を列挙
        return [
            f"{sp['speakerName']}（{st['styleName']}）  "
            f"speaker_uuid: {sp['speakerUuid']}  style_id: {st['styleId']}"
            for sp in speakers
            for st in sp.get("styles", [])
        ]


# エンジン種別とクラスの対応表
_ENGINE_CLASSES: dict[str, type[TTSEngine]] = {
    "voicevox": VoicevoxEngine,
    "coeiroink": CoeiroinkEngine,
}


def create_engines(
    configs: dict[str, EngineConfig], session: aiohttp.ClientSession
) -> dict[str, TTSEngine]:
    """設定からエンジン名 → インスタンスの辞書を生成する。"""
    return {
        name: _ENGINE_CLASSES[cfg.type](cfg, session)
        for name, cfg in configs.items()
    }
