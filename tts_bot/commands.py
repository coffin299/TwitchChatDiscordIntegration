"""スラッシュコマンドの定義。"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .config import ConfigError, normalize_channel
from .guild_speaker import GuildSpeaker

if TYPE_CHECKING:
    from .bot import TwitchTTSBot

log = logging.getLogger(__name__)

# 複数チャンネル指定時の区切り（カンマ・読点・空白）
_CHANNEL_SEPARATOR = re.compile(r"[\s,、]+")


def parse_channels(raw: str) -> tuple[str, ...]:
    """「URL またはチャンネル名」の羅列を正規化済みチャンネルのタプルにする。"""
    items = [item for item in _CHANNEL_SEPARATOR.split(raw) if item]
    # 1 つも無ければ入力ミス
    if not items:
        raise ConfigError("Twitch の URL またはチャンネル名を入力してください")
    # 正規化しつつ重複を除去（順序は維持）
    return tuple(dict.fromkeys(normalize_channel(item) for item in items))


def _format_channels(channels: tuple[str, ...]) -> str:
    """チャンネル一覧を表示用の文字列にする。"""
    return ", ".join(f"#{c}" for c in channels)


def register_commands(bot: TwitchTTSBot) -> None:
    """全スラッシュコマンドを CommandTree に登録する。"""
    bot.tree.add_command(_build_tts_group(bot))
    bot.tree.add_command(_build_setting_command(bot))
    bot.tree.add_command(_build_reload_command(bot))


def _build_tts_group(bot: TwitchTTSBot) -> app_commands.Group:
    """/tts グループ（join / leave / skip / status）を作る。"""
    group = app_commands.Group(
        name="tts", description="Twitch コメント読み上げ", guild_only=True
    )

    async def require_session(
        interaction: discord.Interaction,
    ) -> GuildSpeaker | None:
        """このサーバーの読み上げセッションを取得し、無ければ通知する。"""
        speaker = bot.session_for(interaction.guild_id)
        if speaker is None:
            await interaction.response.send_message(
                "読み上げ中ではありません。`/tts join` で開始してください。",
                ephemeral=True,
            )
        return speaker

    @group.command(
        name="join", description="あなたのいる VC で Twitch コメントを読み上げます"
    )
    @app_commands.describe(
        user="誰の Twitch を読むか（省略時は自分）"
    )
    async def join(
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        target = user or interaction.user
        profile = bot.config.users.get(target.id)
        # 読む対象の Twitch が未登録なら登録を案内
        if profile is None:
            who = "あなた" if target == interaction.user else target.mention
            await interaction.response.send_message(
                f"{who} の Twitch は未登録です。"
                "`/tts_setting twitch:<URL>` で登録してください。",
                ephemeral=True,
            )
            return
        member = interaction.user
        # 実行者が VC にいなければ参加先が分からない
        state = member.voice if isinstance(member, discord.Member) else None
        if state is None or state.channel is None:
            await interaction.response.send_message(
                "先にボイスチャンネルに入ってください。", ephemeral=True
            )
            return
        # VC 接続は 3 秒を超えることがあるため先に応答を保留する
        await interaction.response.defer()
        vc = interaction.guild.voice_client if interaction.guild else None
        # 既に接続中なら移動、未接続なら新規接続
        if isinstance(vc, discord.VoiceClient):
            await vc.move_to(state.channel)
        else:
            await state.channel.connect(self_deaf=True)
        # このサーバーで対象ユーザーの Twitch の読み上げを開始
        bot.start_session(state.channel.guild.id, profile)
        await interaction.followup.send(
            f"🔊 {state.channel.mention} で "
            f"{_format_channels(profile.twitch_channels)} の読み上げを開始します。"
        )

    @group.command(name="leave", description="ボイスチャンネルから退出します")
    async def leave(interaction: discord.Interaction) -> None:
        # 退出前の接続状態を控えておく
        vc = interaction.guild.voice_client if interaction.guild else None
        # セッションが無くても VC に残っていれば退出させる
        had_session = await bot.end_session(interaction.guild_id)
        if not had_session and vc is None:
            await interaction.response.send_message(
                "接続していません。", ephemeral=True
            )
            return
        await interaction.response.send_message("👋 退出しました。")

    @group.command(
        name="skip", description="再生中の読み上げを止め、待機中も全て破棄します"
    )
    async def skip(interaction: discord.Interaction) -> None:
        speaker = await require_session(interaction)
        if speaker is None:
            return
        cleared = speaker.clear()
        speaker.skip()
        await interaction.response.send_message(
            f"⏭ スキップしました（破棄 {cleared} 件）。"
        )

    @group.command(name="status", description="このサーバーの読み上げ状況を表示します")
    async def status(interaction: discord.Interaction) -> None:
        speaker = await require_session(interaction)
        if speaker is None:
            return
        profile = speaker.profile
        vc = interaction.guild.voice_client if interaction.guild else None
        # 対象ユーザー・Twitch・VC・エンジン・キュー件数をまとめて表示
        lines = [
            f"対象: <@{profile.discord_user_id}>",
            f"Twitch: {_format_channels(profile.twitch_channels)}",
            f"VC: {vc.channel.mention if vc else '未接続'}",
            f"エンジン: {profile.voice.engine}",
            f"待機中: {speaker.queue_size} 件",
        ]
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    return group


def _build_setting_command(bot: TwitchTTSBot) -> app_commands.Command:
    """/tts_setting（Discord ユーザー ⇔ Twitch の登録＋ホットリロード）を作る。"""

    @app_commands.command(
        name="tts_setting",
        description="あなたの Discord アカウントと Twitch チャンネルを紐付けます",
    )
    @app_commands.describe(
        twitch="Twitch の URL またはチャンネル名（カンマ区切りで複数可）",
        user="登録対象の Discord ユーザー（省略時は自分。他人は Bot オーナーのみ）",
    )
    async def tts_setting(
        interaction: discord.Interaction,
        twitch: str,
        user: discord.User | None = None,
    ) -> None:
        target = user or interaction.user
        # 他人の登録は Bot オーナーに限定する
        is_other = target.id != interaction.user.id
        if is_other and not await bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "他のユーザーの登録は Bot オーナーのみ行えます。", ephemeral=True
            )
            return

        # 入力を正規化（不正なら書き込み前に弾く）
        try:
            channels = parse_channels(twitch)
        except ConfigError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        # ファイル書き込み・リロードで時間がかかるため応答を保留
        await interaction.response.defer(ephemeral=True)
        try:
            await bot.save_user_setting(target.id, channels)
        except (ConfigError, OSError) as exc:
            log.warning("tts_setting に失敗: %s", exc)
            await interaction.followup.send(f"❌ 保存に失敗しました: {exc}")
            return

        await interaction.followup.send(
            "✅ config.yaml に保存し、ホットリロードしました。\n"
            f"{target.mention} ⇔ Twitch: {_format_channels(channels)}\n"
            "VC に入って `/tts join` すると読み上げを開始します。"
        )

    return tts_setting


def _build_reload_command(bot: TwitchTTSBot) -> app_commands.Command:
    """/tts_reload（config.yaml の手動編集を反映）を作る。"""

    @app_commands.command(
        name="tts_reload", description="config.yaml を再読み込みします"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def tts_reload(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            config = await bot.reload_config()
        except (ConfigError, OSError) as exc:
            # 失敗時は現在の設定のまま動き続ける
            await interaction.followup.send(
                f"❌ 再読み込みに失敗しました（現在の設定を維持）: {exc}"
            )
            return
        await interaction.followup.send(
            f"✅ 再読み込みしました（{len(config.users)} ユーザー）。"
        )

    return tts_reload
