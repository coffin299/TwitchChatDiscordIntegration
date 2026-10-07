"""Discord Bot 本体。Twitch 受信と各サーバーの読み上げを束ねる。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable

import aiohttp
import discord
from discord import app_commands

from .commands import register_commands
from .config import AppConfig, load_config
from .config_writer import upsert_server
from .guild_speaker import GuildSpeaker
from .tts_engines import TTSEngine, TTSError, create_engines
from .twitch_irc import ChatMessage, TwitchChatClient

log = logging.getLogger(__name__)


class TwitchTTSBot(discord.Client):
    """Twitch コメントを Discord の VC で読み上げる Bot。"""

    def __init__(self, config: AppConfig, config_path: str | Path) -> None:
        # サーバー情報と VC 状態だけ受け取れば十分（特権 Intent 不要）
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.config = config
        self.config_path = Path(config_path)
        self.tree = app_commands.CommandTree(self)
        self._http: aiohttp.ClientSession | None = None
        self._engines: dict[str, TTSEngine] = {}
        # guild_id → 読み上げ担当
        self._speakers: dict[int, GuildSpeaker] = {}
        # Twitch チャンネル → 読み上げ担当のリスト（1 配信を複数鯖で読むケース）
        self._routes: dict[str, list[GuildSpeaker]] = {}
        self._twitch_task: asyncio.Task[None] | None = None
        # 現在 IRC で受信中のチャンネル集合（変化時のみ再接続する）
        self._twitch_channels: frozenset[str] = frozenset()
        # 設定ファイルの書き込み・再読み込みを直列化するロック
        self._config_lock = asyncio.Lock()
        # Bot オーナー（チーム所有ならメンバー全員）の ID キャッシュ
        self._owner_ids: frozenset[int] | None = None
        self._commands_synced = False

    def _voice_client_getter(
        self, guild_id: int
    ) -> Callable[[], discord.VoiceClient | None]:
        """指定サーバーの現在の VoiceClient を返す関数を作る。"""

        def getter() -> discord.VoiceClient | None:
            guild = self.get_guild(guild_id)
            # サーバー未取得（起動直後など）なら未接続扱い
            if guild is None:
                return None
            vc = guild.voice_client
            # 本 Bot は discord.VoiceClient のみ使用する
            return vc if isinstance(vc, discord.VoiceClient) else None

        return getter

    # ------------------------------------------------------------
    # 起動・設定反映
    # ------------------------------------------------------------
    async def setup_hook(self) -> None:
        """ログイン後・接続前の初期化処理。"""
        self._http = aiohttp.ClientSession()
        register_commands(self)
        await self._apply_config(self.config, check_engines=True)

    async def _apply_config(
        self, config: AppConfig, check_engines: bool
    ) -> None:
        """設定を読み上げ担当・Twitch 受信へ反映する（起動時・リロード時共通）。"""
        self.config = config
        self._engines = create_engines(config.engines, self._http)
        # エンジン設定が変わったときだけ疎通確認する
        if check_engines:
            await self._check_engines()

        new_ids = {server.guild_id for server in config.servers}
        # 設定から消えたサーバーは読み上げを止めて VC から退出
        for guild_id in [g for g in self._speakers if g not in new_ids]:
            self._speakers.pop(guild_id).stop()
            vc = self._voice_client_getter(guild_id)()
            if vc is not None:
                await vc.disconnect(force=False)

        # 既存サーバーは設定を差し替え、新規サーバーは担当を生成
        for server in config.servers:
            speaker = self._speakers.get(server.guild_id)
            if speaker is not None:
                speaker.update(server, self._engines, config.ffmpeg_path)
                continue
            speaker = GuildSpeaker(
                server,
                self._engines,
                self._voice_client_getter(server.guild_id),
                config.ffmpeg_path,
            )
            speaker.start()
            self._speakers[server.guild_id] = speaker

        # Twitch チャンネル → 読み上げ担当の経路を作り直す
        routes: dict[str, list[GuildSpeaker]] = defaultdict(list)
        for speaker in self._speakers.values():
            for channel in speaker.server.twitch_channels:
                routes[channel].append(speaker)
        self._routes = dict(routes)
        self._restart_twitch_if_needed()

    def _restart_twitch_if_needed(self) -> None:
        """受信チャンネルが変わったときだけ IRC 接続を張り直す。"""
        channels = frozenset(self._routes)
        task = self._twitch_task
        running = task is not None and not task.done()
        # チャンネル集合が同じで接続も生きていれば何もしない
        if running and channels == self._twitch_channels:
            return
        if self._twitch_task is not None:
            self._twitch_task.cancel()
        self._twitch_channels = channels
        # 1 チャンネルも無ければ接続しない
        if not channels:
            self._twitch_task = None
            return
        # 全チャンネルを 1 本の IRC 接続でまとめて受信
        twitch = TwitchChatClient(sorted(channels), self._on_twitch_message)
        self._twitch_task = asyncio.create_task(twitch.run_forever())

    async def _check_engines(self) -> None:
        """各エンジンへ疎通確認し、結果をログに出す。"""
        for name, engine in self._engines.items():
            try:
                count = len(await engine.list_speakers())
                log.info(
                    "エンジン %s (%s) 接続OK: %d スタイル",
                    name, engine.config.url, count,
                )
            except TTSError as exc:
                # 起動は止めず、後から起動されたエンジンにも対応できるようにする
                log.warning("エンジン %s に接続できません: %s", name, exc)

    async def _hot_reload_locked(self) -> AppConfig:
        """設定ファイルを読み直して反映する（呼び出し側でロック取得済み）。"""
        config = await asyncio.to_thread(load_config, self.config_path)
        # エンジン定義に変更があるときだけ疎通確認する
        changed = config.engines != self.config.engines
        await self._apply_config(config, check_engines=changed)
        await self._auto_join()
        log.info("設定をホットリロードしました（%d サーバー）", len(config.servers))
        return config

    async def reload_config(self) -> AppConfig:
        """config.yaml を再読み込みして反映する。"""
        async with self._config_lock:
            return await self._hot_reload_locked()

    async def save_server_setting(
        self,
        guild_id: int,
        channels: tuple[str, ...],
        voice_channel_id: int | None,
    ) -> AppConfig:
        """config.yaml にサーバー設定を書き込み、そのままホットリロードする。"""
        async with self._config_lock:
            # ファイル I/O はイベントループを止めないよう別スレッドで実行
            await asyncio.to_thread(
                upsert_server,
                self.config_path,
                guild_id,
                channels,
                voice_channel_id,
            )
            return await self._hot_reload_locked()

    # ------------------------------------------------------------
    # Discord イベント
    # ------------------------------------------------------------
    async def on_ready(self) -> None:
        """接続完了時にコマンドを同期し、設定された VC へ自動参加する。"""
        log.info("Discord にログインしました: %s", self.user)
        # 再接続のたびに on_ready が来るため同期は初回のみ
        if not self._commands_synced:
            self._commands_synced = True
            for guild in self.guilds:
                await self._sync_commands(guild)
        await self._auto_join()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """新しく招待されたサーバーでもすぐコマンドを使えるようにする。"""
        await self._sync_commands(guild)

    async def _sync_commands(self, guild: discord.abc.Snowflake) -> None:
        """スラッシュコマンドをサーバー単位で即時同期する。"""
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            log.warning("サーバー %s へのコマンド同期失敗: %s", guild.id, exc)

    async def _auto_join(self) -> None:
        """voice_channel_id が設定されたサーバーの VC へ参加する。"""
        for server in self.config.servers:
            # 自動参加先が無いサーバーはスキップ
            if server.voice_channel_id is None:
                continue
            # 既に接続済みなら何もしない（再接続・リロード時の対策）
            if self._voice_client_getter(server.guild_id)() is not None:
                continue
            channel = self.get_channel(server.voice_channel_id)
            # VC 以外の ID が書かれていたら警告
            voice_types = (discord.VoiceChannel, discord.StageChannel)
            if not isinstance(channel, voice_types):
                log.warning(
                    "voice_channel_id %s は VC ではありません",
                    server.voice_channel_id,
                )
                continue
            try:
                await channel.connect(self_deaf=True)
                log.info("VC %s に自動参加しました", channel.name)
            except (discord.ClientException, asyncio.TimeoutError) as exc:
                log.warning("VC %s への自動参加に失敗: %s", channel.name, exc)

    def _on_twitch_message(self, message: ChatMessage) -> None:
        """Twitch のチャットを対応する全サーバーへ配送する。"""
        for speaker in self._routes.get(message.channel, ()):
            speaker.handle_chat(message)

    # ------------------------------------------------------------
    # コマンドから使う補助
    # ------------------------------------------------------------
    def speaker_for(self, guild_id: int | None) -> GuildSpeaker | None:
        """サーバー ID から読み上げ担当を取得する（未設定サーバーは None）。"""
        return self._speakers.get(guild_id) if guild_id is not None else None

    async def is_owner(self, user: discord.abc.User) -> bool:
        """ユーザーが Bot のオーナー（チーム所有ならメンバー）か判定する。"""
        # 初回だけアプリ情報を取得してキャッシュ
        if self._owner_ids is None:
            info = await self.application_info()
            if info.team is not None:
                self._owner_ids = frozenset(m.id for m in info.team.members)
            else:
                self._owner_ids = frozenset({info.owner.id})
        return user.id in self._owner_ids

    async def close(self) -> None:
        """終了時に Twitch 受信と HTTP セッションを片付ける。"""
        if self._twitch_task is not None:
            self._twitch_task.cancel()
        if self._http is not None:
            await self._http.close()
        await super().close()
