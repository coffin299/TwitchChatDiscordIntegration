"""Discord サーバーごとの読み上げセッション（キューと再生処理）。"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Callable

import discord

from .config import UserConfig, VoiceConfig
from .text_filter import build_speech_text
from .tts_engines import TTSEngine, TTSError
from .twitch_irc import ChatMessage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Listener:
    """セッションに参加している配信者 1 人分の読み上げ設定。"""

    profile: UserConfig
    # /tts join twitch:... で一時指定されたチャンネル（リロード後も維持）
    channel_override: tuple[str, ...] | None = None


class GuildSpeaker:
    """1 つの Discord サーバーの VC で、複数配信者の Twitch コメントを読み上げる。

    配信者ごとに声・エンジン（VOICEVOX / COEIROINK 混在可）・読み方を持ち、
    再生は 1 本のキューで順番に行う（VC の音声出力は 1 系統のため）。
    """

    def __init__(
        self,
        guild_id: int,
        engines: dict[str, TTSEngine],
        get_voice_client: Callable[[], discord.VoiceClient | None],
        ffmpeg_path: str,
    ) -> None:
        self.guild_id = guild_id
        # Discord ユーザー ID → 配信者設定（参加順を維持）
        self.listeners: dict[int, Listener] = {}
        self._engines = engines
        self._get_voice_client = get_voice_client
        self._ffmpeg_path = ffmpeg_path
        # (読み上げテキスト, 声) を溜めるキュー（上限は配信者ごとに判定）
        self._queue: asyncio.Queue[tuple[str, VoiceConfig]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    @property
    def queue_size(self) -> int:
        """未再生のメッセージ数。"""
        return self._queue.qsize()

    @property
    def channels(self) -> frozenset[str]:
        """このセッションで読み上げる全 Twitch チャンネル。"""
        return frozenset(
            channel
            for listener in self.listeners.values()
            for channel in listener.profile.twitch_channels
        )

    def start(self) -> None:
        """再生ワーカーを起動する（多重起動はしない）。"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def set_listener(self, listener: Listener) -> None:
        """配信者を追加する（同じ人が既にいれば設定を置き換える）。"""
        self.listeners[listener.profile.discord_user_id] = listener

    def remove_listener(self, user_id: int) -> bool:
        """配信者を外す。外したら True。"""
        return self.listeners.pop(user_id, None) is not None

    def update_engines(
        self, engines: dict[str, TTSEngine], ffmpeg_path: str
    ) -> None:
        """ホットリロード時にエンジンと ffmpeg を差し替える。"""
        self._engines = engines
        self._ffmpeg_path = ffmpeg_path

    def stop(self) -> None:
        """キューを破棄し、再生ワーカーを停止する。"""
        self.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    def _profile_for_channel(self, channel: str) -> UserConfig | None:
        """Twitch チャンネルを担当する配信者の設定を返す（先に参加した人優先）。"""
        for listener in self.listeners.values():
            if channel in listener.profile.twitch_channels:
                return listener.profile
        return None

    def handle_chat(self, message: ChatMessage) -> None:
        """チャットを、そのチャンネルの配信者の設定で整形してキューへ積む。"""
        vc = self._get_voice_client()
        # VC 未接続中のコメントは溜めずに捨てる（接続時に大量再生されるのを防ぐ）
        if vc is None or not vc.is_connected():
            return
        profile = self._profile_for_channel(message.channel)
        # このセッションで読まないチャンネルなら無視
        if profile is None:
            return
        text = build_speech_text(message, profile.reading)
        # 読み上げ対象外なら何もしない
        if text is None:
            return
        # 配信者ごとのキュー上限を超えていたら破棄
        if self._queue.qsize() >= max(profile.reading.max_queue, 1):
            log.warning("[%s] キューが満杯のため破棄: %s", self.guild_id, text)
            return
        # Twitch 視聴者別の声があれば優先、無ければ配信者の声
        voice = profile.viewer_voices.get(message.login, profile.voice)
        self._queue.put_nowait((text, voice))

    def clear(self) -> int:
        """未再生のキューを全て破棄し、破棄件数を返す。"""
        count = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            count += 1
        return count

    def skip(self) -> bool:
        """再生中の音声を停止する。停止したら True。"""
        vc = self._get_voice_client()
        # 再生中でなければ何もしない
        if vc is None or not vc.is_playing():
            return False
        vc.stop()
        return True

    async def _run(self) -> None:
        """キューから取り出して合成・再生を繰り返すワーカー。"""
        while True:
            text, voice = await self._queue.get()
            try:
                await self._speak(text, voice)
            except TTSError as exc:
                log.error("[%s] 音声合成に失敗: %s", self.guild_id, exc)
            except Exception:  # noqa: BLE001
                log.exception("[%s] 再生中にエラーが発生しました", self.guild_id)
            finally:
                self._queue.task_done()

    @staticmethod
    async def _voice_exists(engine: TTSEngine, voice: VoiceConfig) -> bool:
        """話者の存在を確認する（確認自体に失敗したら存在扱い）。"""
        try:
            return await engine.is_voice_installed(voice)
        except TTSError:
            return True

    async def _speak(self, text: str, voice: VoiceConfig) -> None:
        """1 件分のテキストを合成して VC で再生し、再生完了まで待つ。"""
        engine = self._engines.get(voice.engine)
        # リロードでエンジンが削除された後の古いキューは捨てる
        if engine is None:
            return
        try:
            wav = await engine.synthesize(text, voice)
        except TTSError as exc:
            # 話者未インストールが原因なら分かりやすい理由に置き換える
            if not await self._voice_exists(engine, voice):
                raise TTSError(
                    f"エンジン {voice.engine} に {engine.voice_key(voice)} の"
                    "話者がありません（--list-speakers で確認）"
                ) from exc
            raise
        vc = self._get_voice_client()
        # 合成中に VC から切断されていたら再生しない
        if vc is None or not vc.is_connected():
            return
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        # WAV をメモリから ffmpeg の標準入力へ流し込む
        source = discord.FFmpegPCMAudio(
            io.BytesIO(wav), pipe=True, executable=self._ffmpeg_path
        )
        # after コールバックは別スレッドで呼ばれるため thread-safe に通知
        vc.play(
            source,
            after=lambda _err: loop.call_soon_threadsafe(finished.set),
        )
        log.debug("[%s] 読み上げ: %s", self.guild_id, text)
        await finished.wait()
