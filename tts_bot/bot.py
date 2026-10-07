"""Discord Bot 本体。Twitch 受信と各サーバーの読み上げセッションを束ねる。"""

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
from .config import AppConfig, UserConfig, load_config
from .config_writer import upsert_user
from .guild_speaker import GuildSpeaker
from .tts_engines import TTSEngine, TTSError, create_engines
from .twitch_irc import ChatMessage, TwitchChatClient

log = logging.getLogger(__name__)


class TwitchTTSBot(discord.Client):
    """Twitch コメントを Discord の VC で読み上げる Bot。

    /tts join した人（Discord ユーザー）に紐付く Twitch のコメントを、
    その人がいるサーバーの VC で読み上げる。サーバー ID の設定は不要。
    """

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
        # guild_id → 読み上げセッション（1 サーバーで同時に 1 つ）
        self._sessions: dict[int, GuildSpeaker] = {}
        # Twitch チャンネル → セッションのリスト（1 配信を複数鯖で読むケース）
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
            # サーバー未取得なら未接続扱い
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
        """設定をエンジン・稼働中セッションへ反映する（起動時・リロード時共通）。"""
        self.config = config
        self._engines = create_engines(config.engines, self._http)
        # エンジン設定が変わったときだけ疎通確認する
        if check_engines:
            await self._check_engines()
        # 稼働中セッションは最新のユーザー設定に差し替える
        for guild_id, speaker in list(self._sessions.items()):
            profile = config.users.get(speaker.profile.discord_user_id)
            # 設定から消えたユーザーのセッションは終了して退出
            if profile is None:
                await self.end_session(guild_id)
                continue
            speaker.update(profile, self._engines, config.ffmpeg_path)
        self._rebuild_routes()

    def _rebuild_routes(self) -> None:
        """稼働中セッションから Twitch チャンネル → 配送先の表を作り直す。"""
        routes: dict[str, list[GuildSpeaker]] = defaultdict(list)
        for speaker in self._sessions.values():
            for channel in speaker.profile.twitch_channels:
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
        if task is not None:
            task.cancel()
        self._twitch_channels = channels
        # 読み上げ中のセッションが無ければ Twitch には接続しない
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
        log.info("設定をホットリロードしました（%d ユーザー）", len(config.users))
        return config

    async def reload_config(self) -> AppConfig:
        """config.yaml を再読み込みして反映する。"""
        async with self._config_lock:
            return await self._hot_reload_locked()

    async def save_user_setting(
        self, user_id: int, channels: tuple[str, ...]
    ) -> AppConfig:
        """config.yaml にユーザー設定を書き込み、そのままホットリロードする。"""
        async with self._config_lock:
            # ファイル I/O はイベントループを止めないよう別スレッドで実行
            await asyncio.to_thread(
                upsert_user, self.config_path, user_id, channels
            )
            return await self._hot_reload_locked()

    # ------------------------------------------------------------
    # 読み上げセッション
    # ------------------------------------------------------------
    def session_for(self, guild_id: int | None) -> GuildSpeaker | None:
        """サーバーで稼働中の読み上げセッションを返す（無ければ None）。"""
        return self._sessions.get(guild_id) if guild_id is not None else None

    def start_session(self, guild_id: int, profile: UserConfig) -> None:
        """サーバーで指定ユーザーの読み上げを開始する（既存は置き換え）。"""
        old = self._sessions.pop(guild_id, None)
        # 同じサーバーで別の人の読み上げ中なら止めてから切り替える
        if old is not None:
            old.stop()
        speaker = GuildSpeaker(
            guild_id,
            profile,
            self._engines,
            self._voice_client_getter(guild_id),
            self.config.ffmpeg_path,
        )
        speaker.start()
        self._sessions[guild_id] = speaker
        self._rebuild_routes()

    async def end_session(
        self, guild_id: int, disconnect: bool = True
    ) -> bool:
        """サーバーの読み上げを終了する。セッションがあれば True。"""
        speaker = self._sessions.pop(guild_id, None)
        if speaker is not None:
            speaker.stop()
            self._rebuild_routes()
        # 必要なら VC からも退出
        vc = self._voice_client_getter(guild_id)()
        if disconnect and vc is not None:
            await vc.disconnect(force=False)
        return speaker is not None

    # ------------------------------------------------------------
    # Discord イベント
    # ------------------------------------------------------------
    async def on_ready(self) -> None:
        """接続完了時にスラッシュコマンドを同期する。"""
        log.info("Discord にログインしました: %s", self.user)
        # 再接続のたびに on_ready が来るため同期は初回のみ
        if not self._commands_synced:
            self._commands_synced = True
            for guild in self.guilds:
                await self._sync_commands(guild)

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

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Bot の切断や、VC に人がいなくなったときにセッションを片付ける。"""
        guild_id = member.guild.id
        # Bot 自身が VC から外された（キック等）ならセッションだけ終了
        if self.user is not None and member.id == self.user.id:
            if after.channel is None:
                await self.end_session(guild_id, disconnect=False)
            return
        vc = self._voice_client_getter(guild_id)()
        # Bot のいる VC から誰かが抜けたときだけ判定する
        if vc is None or before.channel != vc.channel:
            return
        if after.channel == vc.channel:
            return
        # 人間が 1 人もいなくなったら退出
        if not any(not m.bot for m in vc.channel.members):
            log.info("VC %s が無人になったため退出します", vc.channel.name)
            await self.end_session(guild_id)

    def _on_twitch_message(self, message: ChatMessage) -> None:
        """Twitch のチャットを対応する全セッションへ配送する。"""
        for speaker in self._routes.get(message.channel, ()):
            speaker.handle_chat(message)

    # ------------------------------------------------------------
    # コマンドから使う補助
    # ------------------------------------------------------------
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
