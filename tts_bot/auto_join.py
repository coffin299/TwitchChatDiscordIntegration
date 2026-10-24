"""配信開始を検知して、その人がいる VC へ自動参加する。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from .config import UserConfig
from .twitch_live import TwitchLiveChecker, TwitchLiveError

if TYPE_CHECKING:
    from .bot import TwitchTTSBot

log = logging.getLogger(__name__)

# 配信状態を問い合わせる間隔（秒）。VC の入退室では待たずに確認する
_POLL_SECONDS = 15

# 読み上げに載せない絵文字・カスタム絵文字
_CUSTOM_EMOJI = re.compile(r"<a?:[^:]{1,32}:\d+>")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+"
)
_SPACES = re.compile(r"\s+")


def speech_name(raw: str, fallback: str) -> str:
    """表示名を読み上げできる短い文字列にする。空なら fallback。"""
    text = _CUSTOM_EMOJI.sub("", raw)
    text = _EMOJI.sub("", text)
    text = _SPACES.sub(" ", text).strip()
    # 表示名がすでに「さん」で終わっていても「さんさん」と読まない
    if text.endswith("さん") and len(text) > 2:
        text = text[:-2].strip()
    if not text:
        text = fallback
    return text[:40]


def reading_started_announcement(names: list[str]) -> str:
    """開始アナウンス。1 人は「nさんの…」、複数は「nさんとnさんの…」。"""
    who = "と".join(f"{name}さん" for name in names)
    return f"{who}のツイッチコメント読み上げを開始しました"


@dataclass(frozen=True)
class _JoinPlan:
    """1 サーバーで、これから読み上げを始める配信者と参加先。"""

    channel: discord.VoiceChannel
    profiles: tuple[UserConfig, ...]
    need_connect: bool


class AutoJoinController:
    """登録ユーザーの配信開始（または配信中の VC 参加）で読み上げを始める。

    テキストチャンネルへは通知しない。開始は VC の読み上げで知らせる。
    同じ VC に登録配信者が複数いても、配信中の人が 1 人いれば全員を対象にする。
    """

    def __init__(self, bot: TwitchTTSBot) -> None:
        self._bot = bot
        self._checker: TwitchLiveChecker | None = None
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        # 直近で配信中と分かっているログイン名。未取得は None（失敗時は維持）
        self._live: set[str] | None = None
        # /tts stop や /tts leave のあと、配信が終わるか VC を出るまで自動で戻さない
        self._paused: dict[int, set[int]] = {}

    def start(self) -> None:
        """監視ループを起動する（多重起動しない）。"""
        if self._task is not None and not self._task.done():
            return
        http = self._bot._http
        if http is None:
            return
        self._checker = TwitchLiveChecker(http)
        self._task = asyncio.create_task(self._loop(), name="auto-join")

    def stop(self) -> asyncio.Task[None] | None:
        """監視ループを止めて、停止待ちできるタスクを返す。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        return task

    def notify_voice_change(self) -> None:
        """VC の入退室があったので、次の確認を待たずに行う。"""
        self._wakeup.set()

    def pause(self, guild_id: int, user_ids: list[int] | tuple[int, ...]) -> None:
        """このユーザーたちを、条件が変わるまで自動参加の対象から外す。"""
        if not user_ids:
            return
        self._paused.setdefault(guild_id, set()).update(user_ids)

    def unpause(self, guild_id: int, user_id: int) -> None:
        """手動の /tts join で、自動参加の保留を解く。"""
        paused = self._paused.get(guild_id)
        if paused is not None:
            paused.discard(user_id)

    async def _loop(self) -> None:
        while True:
            # 処理中に来た起床要求は、この周が終わった直後にもう一度見る
            self._wakeup.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("配信開始の自動参加でエラーが発生しました")
            try:
                await asyncio.wait_for(self._wakeup.wait(), _POLL_SECONDS)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        config = self._bot.config
        # OFF の間に溜めた配信状態は捨て、ON に戻した最初の確認で参加できるようにする
        if not config.auto_join_on_stream:
            self._live = None
            return
        checker = self._checker
        if checker is None:
            return
        logins = sorted(
            {
                channel
                for user in config.users.values()
                for channel in user.twitch_channels
            }
        )
        if not logins:
            return
        try:
            live = await checker.live_logins(logins)
        except TwitchLiveError as exc:
            # 取得失敗で全員オフライン扱いにすると、復帰時に読み上げを止めたように見える
            log.warning("配信状態を取得できません: %s", exc)
            return
        previously = self._live
        # 起動直後は、すでに配信中で VC にいる人も参加させる
        newly = live if previously is None else live - previously
        self._live = live
        for guild in list(self._bot.guilds):
            if guild.unavailable:
                continue
            try:
                await self._handle_guild(guild, live, newly)
            except Exception:
                log.exception("サーバー %s の自動参加に失敗しました", guild.id)

    def _release_pauses(self, guild: discord.Guild, live: set[str]) -> None:
        """VC を出たか、配信が終わった人の保留を解く。"""
        paused = self._paused.get(guild.id)
        if not paused:
            return
        in_voice: set[int] = set()
        for channel in guild.voice_channels:
            in_voice.update(channel.voice_states)
        for user_id in list(paused):
            profile = self._bot.config.users.get(user_id)
            left = user_id not in in_voice
            offline = profile is None or not any(
                channel in live for channel in profile.twitch_channels
            )
            if left or offline:
                paused.discard(user_id)

    def _user_is_live(self, user_id: int, live: set[str]) -> bool:
        profile = self._bot.config.users.get(user_id)
        if profile is None:
            return False
        return any(channel in live for channel in profile.twitch_channels)

    def _plan(
        self, guild: discord.Guild, live: set[str], newly: set[str]
    ) -> _JoinPlan | None:
        """参加先と、新たに読み上げを始める配信者を決める。"""
        self._release_pauses(guild, live)
        paused = self._paused.get(guild.id, set())
        registered = self._bot.config.users
        groups: list[tuple[discord.VoiceChannel, tuple[int, ...]]] = []
        for channel in guild.voice_channels:
            # voice_states はメンバーキャッシュが無くても在室者の ID が分かる
            user_ids = tuple(
                user_id
                for user_id in channel.voice_states
                if user_id in registered and user_id not in paused
            )
            if user_ids:
                groups.append((channel, user_ids))
        eligible = [
            group
            for group in groups
            if any(self._user_is_live(user_id, live) for user_id in group[1])
        ]
        if not eligible:
            return None
        vc = self._bot._voice_client_getter(guild.id)()
        # 別の VC で読み上げ中なら移動しない（手動参加を奪わない）
        if vc is not None:
            if vc.channel is None:
                return None
            eligible = [
                group for group in eligible if group[0].id == vc.channel.id
            ]
            if not eligible:
                return None
            channel, user_ids = eligible[0]
            need_connect = False
        else:
            # 今まさに配信開始した人がいる VC を優先し、無ければ配信中の VC へ入る
            fresh = [
                group
                for group in eligible
                if any(self._user_is_live(user_id, newly) for user_id in group[1])
            ]
            pool = fresh or eligible
            channel, user_ids = min(pool, key=lambda group: (-len(group[1]), group[0].id))
            need_connect = True
        speaker = self._bot.session_for(guild.id)
        profiles = tuple(
            registered[user_id]
            for user_id in user_ids
            if speaker is None or user_id not in speaker.listeners
        )
        if not profiles:
            return None
        return _JoinPlan(channel, profiles, need_connect)

    async def _names(
        self, guild: discord.Guild, profiles: tuple[UserConfig, ...]
    ) -> dict[int, str]:
        """アナウンスに使う表示名。取れなければ Twitch のログイン名。"""
        names: dict[int, str] = {}
        for profile in profiles:
            member = guild.get_member(profile.discord_user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(profile.discord_user_id)
                except discord.HTTPException:
                    member = None
            fallback = profile.twitch_channels[0]
            raw = member.display_name if member is not None else fallback
            names[profile.discord_user_id] = speech_name(raw, fallback)
        return names

    async def _connect(self, channel: discord.VoiceChannel) -> bool:
        """対象 VC へ入る。すでに同じ VC にいれば何もしない。"""
        vc = self._bot._voice_client_getter(channel.guild.id)()
        if vc is not None and vc.channel is not None and vc.channel.id == channel.id:
            return vc.is_connected()
        if vc is not None:
            return False
        try:
            await channel.connect(self_deaf=True)
        except discord.DiscordException as exc:
            log.warning("VC %s に自動参加できません: %s", channel.name, exc)
            return False
        return True

    async def _handle_guild(
        self,
        guild: discord.Guild,
        live: set[str],
        newly: set[str],
    ) -> None:
        # 接続処理はボイス状態のイベントを待つ。セッション用ロックは取らない
        async with self._bot.session_lock:
            plan = self._plan(guild, live, newly)
        if plan is None:
            return
        names = await self._names(guild, plan.profiles)
        if plan.need_connect:
            async with self._bot.connect_lock:
                connected = await self._connect(plan.channel)
            if not connected:
                return
        else:
            current = self._bot._voice_client_getter(guild.id)()
            # 接続完了前にアナウンスを積むと、未接続扱いで捨てられる
            if current is None or not current.is_connected():
                return
        async with self._bot.session_lock:
            # 接続している間に /tts stop や退室があったら、その人は足さない
            still = tuple(
                profile
                for profile in plan.profiles
                if self._still_wanted(guild, plan.channel, profile.discord_user_id)
            )
            if not still:
                should_disconnect = self._bot.session_for(guild.id) is None
                vc = self._bot._voice_client_getter(guild.id)()
                speaker = None
                announcement = ""
            else:
                should_disconnect = False
                vc = None
                speaker = None
                added: list[str] = []
                for profile in still:
                    speaker = self._bot.add_listener(guild.id, profile)
                    added.append(
                        names.get(profile.discord_user_id, profile.twitch_channels[0])
                    )
                announcement = reading_started_announcement(added)
                if speaker is not None:
                    # チャットより先に積む（IRC 接続より合成の方が先に終わる）
                    speaker.enqueue(announcement, self._bot.config.default_voice)
        if speaker is not None and announcement:
            log.info("[%s] %s", guild.id, announcement)
        elif should_disconnect and vc is not None:
            await vc.disconnect(force=False)

    def _still_wanted(
        self, guild: discord.Guild, channel: discord.VoiceChannel, user_id: int
    ) -> bool:
        """まだその VC にいて、保留されておらず、未参加の配信者か。"""
        if user_id not in channel.voice_states:
            return False
        if user_id in self._paused.get(guild.id, set()):
            return False
        speaker = self._bot.session_for(guild.id)
        return speaker is None or user_id not in speaker.listeners
