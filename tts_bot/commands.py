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

    async def resolve(
        interaction: discord.Interaction,
    ) -> GuildSpeaker | None:
        """コマンド実行サーバーの読み上げ担当を取得し、無ければ通知する。"""
        speaker = bot.speaker_for(interaction.guild_id)
        if speaker is None:
            await interaction.response.send_message(
                "このサーバーは未登録です。`/tts_setting` で Twitch を設定してください。",
                ephemeral=True,
            )
        return speaker

    @group.command(name="join", description="あなたがいるボイスチャンネルに参加します")
    async def join(interaction: discord.Interaction) -> None:
        if await resolve(interaction) is None:
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
        await interaction.followup.send(
            f"🔊 {state.channel.mention} で読み上げを開始します。"
        )

    @group.command(name="leave", description="ボイスチャンネルから退出します")
    async def leave(interaction: discord.Interaction) -> None:
        speaker = await resolve(interaction)
        if speaker is None:
            return
        vc = interaction.guild.voice_client if interaction.guild else None
        # 未接続なら何もしない
        if vc is None:
            await interaction.response.send_message(
                "接続していません。", ephemeral=True
            )
            return
        # 残りのキューを捨ててから切断
        speaker.clear()
        await vc.disconnect(force=False)
        await interaction.response.send_message("👋 退出しました。")

    @group.command(
        name="skip", description="再生中の読み上げを止め、待機中も全て破棄します"
    )
    async def skip(interaction: discord.Interaction) -> None:
        speaker = await resolve(interaction)
        if speaker is None:
            return
        cleared = speaker.clear()
        speaker.skip()
        await interaction.response.send_message(
            f"⏭ スキップしました（破棄 {cleared} 件）。"
        )

    @group.command(name="status", description="このサーバーの読み上げ設定を表示します")
    async def status(interaction: discord.Interaction) -> None:
        speaker = await resolve(interaction)
        if speaker is None:
            return
        server = speaker.server
        vc = interaction.guild.voice_client if interaction.guild else None
        channels = ", ".join(f"#{c}" for c in server.twitch_channels)
        # VC 接続先・Twitch チャンネル・エンジン・キュー件数をまとめて表示
        lines = [
            f"Twitch: {channels}",
            f"VC: {vc.channel.mention if vc else '未接続'}",
            f"エンジン: {server.voice.engine}",
            f"待機中: {speaker.queue_size} 件",
        ]
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    return group


def _build_setting_command(bot: TwitchTTSBot) -> app_commands.Command:
    """/tts_setting（config.yaml への登録＋ホットリロード）を作る。"""

    @app_commands.command(
        name="tts_setting",
        description="Discord サーバーと Twitch チャンネルの対応を登録します",
    )
    @app_commands.describe(
        twitch="Twitch の URL またはチャンネル名（カンマ区切りで複数可）",
        voice_channel="起動時・登録時に自動参加する VC（任意）",
        guild_id="対象の Discord サーバー ID（省略時はこのサーバー）",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def tts_setting(
        interaction: discord.Interaction,
        twitch: str,
        voice_channel: discord.VoiceChannel | None = None,
        guild_id: str | None = None,
    ) -> None:
        # 対象サーバー ID を決定（省略時は実行サーバー）
        target_id = interaction.guild_id
        if guild_id:
            # Discord の ID は数字のみ
            if not guild_id.strip().isdigit():
                await interaction.response.send_message(
                    "guild_id は数字で入力してください。", ephemeral=True
                )
                return
            target_id = int(guild_id)

        # 他サーバーの設定変更は Bot オーナーに限定する
        is_other_guild = target_id != interaction.guild_id
        if is_other_guild and not await bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "他サーバーの設定は Bot オーナーのみ変更できます。", ephemeral=True
            )
            return
        # VC 選択肢は実行サーバーのものなので、他サーバー指定とは併用不可
        if is_other_guild and voice_channel is not None:
            await interaction.response.send_message(
                "他サーバー指定時は voice_channel を指定できません。",
                ephemeral=True,
            )
            return

        # 入力を正規化（不正なら書き込み前に弾く）
        try:
            channels = parse_channels(twitch)
        except ConfigError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        # ファイル書き込み・リロード・VC 参加で時間がかかるため応答を保留
        await interaction.response.defer(ephemeral=True)
        vc_id = voice_channel.id if voice_channel else None
        try:
            await bot.save_server_setting(target_id, channels, vc_id)
        except (ConfigError, OSError) as exc:
            log.warning("tts_setting に失敗: %s", exc)
            await interaction.followup.send(f"❌ 保存に失敗しました: {exc}")
            return

        # 結果を表示（未参加なら /tts join を案内）
        names = ", ".join(f"#{c}" for c in channels)
        lines = [
            "✅ config.yaml に保存し、ホットリロードしました。",
            f"Discord: `{target_id}` ⇔ Twitch: {names}",
        ]
        if voice_channel is not None:
            lines.append(f"自動参加 VC: {voice_channel.mention}")
        elif bot.get_guild(target_id) is None:
            lines.append("⚠ Bot はまだこのサーバーに参加していません。")
        else:
            lines.append("VC で `/tts join` すると読み上げを開始します。")
        await interaction.followup.send("\n".join(lines))

    return tts_setting


def _build_reload_command(bot: TwitchTTSBot) -> app_commands.Command:
    """/tts_reload（config.yaml の手動編集を反映）を作る。"""

    @app_commands.command(
        name="tts_reload", description="config.yaml を再読み込みします"
    )
    @app_commands.guild_only()
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
            f"✅ 再読み込みしました（{len(config.servers)} サーバー）。"
        )

    return tts_reload
