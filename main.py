"""エントリーポイント。

使い方:
    python -B main.py                    # Bot を起動
    python -B main.py --list-speakers    # 各エンジンの話者 ID 一覧を表示
    python -B main.py -c other.yaml      # 別の設定ファイルを使う
"""

from __future__ import annotations

import sys

# __pycache__ を作らないよう、パッケージ読み込み前に無効化する
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402

import aiohttp  # noqa: E402
import discord  # noqa: E402

from tts_bot.bot import TwitchTTSBot  # noqa: E402
from tts_bot.config import AppConfig, ConfigError, load_config  # noqa: E402
from tts_bot.tts_engines import TTSError, create_engines  # noqa: E402


# Discord ログイン失敗時に表示する対処法
_LOGIN_FAILURE_HELP = """\
Discord へのログインに失敗しました（トークンが不正です）。
  1. Developer Portal → 対象アプリ → Bot → Reset Token で新しいトークンを発行
     ※ OAuth2 の Client Secret / Application ID / Public Key ではありません
  2. config.yaml の discord.token に貼り付ける（または環境変数 DISCORD_TOKEN）
     ※ ${DISCORD_TOKEN} のままなら環境変数側が古い・誤っている可能性があります
  3. トークンを Discord や GitHub に貼るとすぐ無効化されます。再発行してください"""


async def list_speakers(config: AppConfig) -> None:
    """設定済みエンジンの話者一覧を標準出力に表示する。"""
    async with aiohttp.ClientSession() as session:
        for name, engine in create_engines(config.engines, session).items():
            print(f"=== {name} ({engine.config.type}) {engine.config.url} ===")
            try:
                for line in await engine.list_speakers():
                    print(f"  {line}")
            except TTSError as exc:
                # 1 つのエンジンが落ちていても他は表示する
                print(f"  接続できません: {exc}")


def main() -> int:
    """コマンドライン引数を解釈して Bot 起動または話者一覧表示を行う。"""
    parser = argparse.ArgumentParser(description="Twitch コメント読み上げ Bot")
    parser.add_argument("-c", "--config", default="config.yaml", help="設定ファイル")
    parser.add_argument(
        "--list-speakers", action="store_true", help="話者 ID 一覧を表示して終了"
    )
    args = parser.parse_args()

    # 設定エラーはスタックトレースではなく分かりやすく表示
    try:
        # 話者一覧の確認だけならトークン未設定でも動かせるようにする
        config = load_config(args.config, require_token=not args.list_speakers)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 1

    # 話者一覧モードは Discord に接続せずに終了
    if args.list_speakers:
        asyncio.run(list_speakers(config))
        return 0

    # 不正なログレベル名は INFO にフォールバック
    level = getattr(logging, config.log_level, logging.INFO)
    # 設定ファイルのパスは /tts_setting の書き込み・ホットリロードで使う
    bot = TwitchTTSBot(config, args.config)
    try:
        # root_logger=True で本 Bot 自身のログも discord.py の書式で標準出力へ出す
        bot.run(config.discord_token, log_level=level, root_logger=True)
    except discord.LoginFailure:
        # トークン誤りはスタックトレースではなく対処法を表示
        print(_LOGIN_FAILURE_HELP, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
