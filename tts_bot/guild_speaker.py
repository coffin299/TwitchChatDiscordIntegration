"""Discord サーバーごとの読み上げセッション（キューと再生処理）。"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Callable

import discord

from .config import UserConfig, VoiceConfig
from .text_filter import build_speech_text
from .tts_engines import TTSEngine, TTSError
from .twitch_irc import ChatMessage

log = logging.getLogger(__name__)


class GuildSpeaker:
    """1 つの Discord サーバーの VC で、1 人分の Twitch コメントを読み上げる。"""

    def __init__(
        self,
        guild_id: int,
        profile: UserConfig,
        engines: dict[str, TTSEngine],
        get_voice_client: Callable[[], discord.VoiceClient | None],
        ffmpeg_path: str,
        channel_override: tuple[str, ...] | None = None,
    ) -> None:
        self.guild_id = guild_id
        # /tts join で指定された Discord ユーザーの設定
        self.profile = profile
        # /tts join twitch:... で一時指定されたチャンネル（リロード後も維持）
        self.channel_override = channel_override
        self._engines = engines
        self._get_voice_client = get_voice_client
        self._ffmpeg_path = ffmpeg_path
        # (読み上げテキスト, 声) を溜めるキュー（上限超過分は破棄）
        self._queue: asyncio.Queue[tuple[str, VoiceConfig]] = asyncio.Queue(
            maxsize=max(profile.reading.max_queue, 1)
        )
        self._worker: asyncio.Task[None] | None = None

    @property
    def queue_size(self) -> int:
        """未再生のメッセージ数。"""
        return self._queue.qsize()

    def start(self) -> None:
        """再生ワーカーを起動する（多重起動はしない）。"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def update(
        self,
        profile: UserConfig,
        engines: dict[str, TTSEngine],
        ffmpeg_path: str,
    ) -> None:
        """ホットリロード時に設定を差し替える（キューと再生は継続）。"""
        self.profile = profile
        self._engines = engines
        self._ffmpeg_path = ffmpeg_path

    def stop(self) -> None:
        """キューを破棄し、再生ワーカーを停止する。"""
        self.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    def handle_chat(self, message: ChatMessage) -> None:
        """チャットを整形してキューへ積む。"""
        vc = self._get_voice_client()
        # VC 未接続中のコメントは溜めずに捨てる（接続時に大量再生されるのを防ぐ）
        if vc is None or not vc.is_connected():
            return
        text = build_speech_text(message, self.profile.reading)
        # 読み上げ対象外なら何もしない
        if text is None:
            return
        # Twitch 視聴者別の声があれば優先
        voice = self.profile.viewer_voices.get(
            message.login, self.profile.voice
        )
        try:
            self._queue.put_nowait((text, voice))
        except asyncio.QueueFull:
            log.warning("[%s] キューが満杯のため破棄: %s", self.guild_id, text)

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

    async def _speak(self, text: str, voice: VoiceConfig) -> None:
        """1 件分のテキストを合成して VC で再生し、再生完了まで待つ。"""
        engine = self._engines.get(voice.engine)
        # リロードでエンジンが削除された後の古いキューは捨てる
        if engine is None:
            return
        wav = await engine.synthesize(text, voice)
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
