# TwitchChatDiscordIntegration

Twitch のチャットを **VOICEVOX / COEIROINK** で音声合成し、**Discord のボイスチャンネル**で読み上げる Bot です。

配信PCに負荷をかけないよう、**サブPCで Bot と音声合成エンジンを常駐**させる構成を想定しています。
1 つの Bot で複数の Discord サーバー（身内鯖）を同時に扱え、`Discord サーバーID : Twitch チャンネル` の対応を YAML で管理します。

```
[配信PC]  Discord に参加して聞くだけ（合成処理なし）
    ▲
    │ Discord VC
    │
[サブPC]  本 Bot ──HTTP──▶ VOICEVOX / COEIROINK
    ▲
    │ Twitch IRC（匿名・読み取り専用）
[Twitch チャット]
```

## 機能

- Twitch チャットを匿名接続で受信（Twitch のトークン不要）
- VOICEVOX 互換 API（VOICEVOX / COEIROINK v1 / SHAREVOX など）と COEIROINK v2 API に対応
- 複数 Discord サーバーを 1 プロセスで運用
  - 1 サーバーで複数 Twitch チャンネルを読む / 1 チャンネルを複数サーバーで読む、どちらも可
- サーバーごと・Twitch ユーザーごとに話者・話速などを変更
- エモート除去、URL 省略、長文省略、読み替え辞書、無視ユーザー、コマンド（`!` 始まり）無視
- 起動時に指定 VC へ自動参加（任意）
- **`/tts_setting` で Discord から Twitch チャンネルを登録** → `config.yaml` に保存して即ホットリロード（再起動不要）
- スラッシュコマンド: `/tts join` `/tts leave` `/tts skip` `/tts status` `/tts_setting` `/tts_reload`

## 必要なもの（サブPC）

| 項目 | 備考 |
| :--- | :--- |
| Python 3.11 以上 | |
| ffmpeg | PATH を通すか `ffmpeg_path` で指定（例: `winget install ffmpeg`） |
| VOICEVOX または COEIROINK | 起動しておく。エンジンだけでも可 |
| Discord Bot | [Developer Portal](https://discord.com/developers/applications) で作成 |

## セットアップ

### 1. Discord Bot を作成して招待

1. Developer Portal で Application を作成し、**Bot** タブでトークンを発行
2. 特権 Intent（Message Content など）は **不要**
3. **OAuth2 → URL Generator** で以下を選び、生成 URL から各サーバーへ招待
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `View Channels`

### 2. 仮想環境（.venv）と依存パッケージ

`start.bat` を使う場合は **初回起動時に `.venv` の作成と依存パッケージのインストールを自動で行う** ため、この手順は不要です。

手動で行う場合:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --no-compile -r requirements.txt
```

### 3. 設定ファイルを作成

```powershell
Copy-Item config.example.yaml config.yaml
```

`config.yaml` を編集します。最小構成は以下だけです。`servers` は空のままでも起動でき、後から Discord 上の `/tts_setting` で登録できます。

```yaml
discord:
  token: ${DISCORD_TOKEN}      # 環境変数から読む（直書きも可）

engines:
  voicevox:
    type: voicevox
    url: http://127.0.0.1:50021

servers:
  111111111111111111: streamer_a    # DiscordサーバーID: Twitchチャンネル名
  222222222222222222: https://www.twitch.tv/streamer_b   # URL でも可
```

Discord のサーバー ID / チャンネル ID は、Discord の「開発者モード」を ON にして右クリック →「ID をコピー」で取得できます。

### 4. 話者 ID を確認

エンジンを起動した状態で実行すると、設定に書ける ID が一覧表示されます（トークン未設定でも可）。

```powershell
python -B main.py --list-speakers
```

```
=== voicevox (voicevox) http://127.0.0.1:50021 ===
  四国めたん（ノーマル）  speaker: 2
  ずんだもん（ノーマル）  speaker: 3
=== coeiroink (coeiroink) http://127.0.0.1:50032 ===
  つくよみちゃん（れいせい）  speaker_uuid: 3c37646f-...  style_id: 0
```

### 5. 起動

```powershell
python -B main.py
```

`start.bat` をダブルクリックしても起動できます（`.venv` が無ければ作成し、依存パッケージが足りなければ `.venv` にインストールしてから起動します）。手動で `.venv` を作った場合は `python` を `.venv\Scripts\python.exe` に読み替えてください。`-B` は `__pycache__` を作らないためのオプションです（`main.py` 内でも無効化済み）。

## 使い方

1. （未登録なら）`/tts_setting twitch:https://www.twitch.tv/streamer_a` でこのサーバーと Twitch を紐付け
2. 読み上げを聞きたい VC に入り、`/tts join` を実行（または `voice_channel_id` で自動参加）
3. 対応する Twitch チャンネルのコメントが読み上げられます
4. 配信PCでは Discord でその VC に参加していれば OK

| コマンド | 権限 | 内容 |
| :--- | :--- | :--- |
| `/tts join` | 全員 | 実行者がいる VC に参加（参加中なら移動） |
| `/tts leave` | 全員 | VC から退出し、待機中のコメントを破棄 |
| `/tts skip` | 全員 | 再生中の読み上げを止め、待機中も全て破棄 |
| `/tts status` | 全員 | Twitch チャンネル・VC・エンジン・待機件数を表示 |
| `/tts_setting` | サーバー管理 | Twitch チャンネルを登録・変更し、`config.yaml` に保存してホットリロード |
| `/tts_reload` | サーバー管理 | `config.yaml` を手動編集した後に再読み込み |

VC に接続していない間のコメントは溜めずに捨てます（参加した瞬間に大量に読まれるのを防ぐため）。

### `/tts_setting` の引数

| 引数 | 必須 | 内容 |
| :--- | :---: | :--- |
| `twitch` | ✅ | Twitch の URL（`https://www.twitch.tv/xxx`）またはチャンネル名。カンマ・空白区切りで複数可 |
| `voice_channel` | | 自動参加する VC。指定するとその場で参加し、次回起動時も自動参加 |
| `guild_id` | | 対象の Discord サーバー ID。省略時は実行したサーバー。**他サーバーを指定できるのは Bot オーナーのみ** |

- 既に登録済みのサーバーなら `twitch`（と指定時は `voice_channel_id`）だけを上書きし、`voice` などの個別設定は残します
- `config.yaml` のコメントは保持されます。保存前に検証し、不正なら書き込みません
- 実行できるのは「サーバー管理」権限を持つユーザーです（サーバー設定 → 連携サービス で変更可）

### ホットリロード

`/tts_setting` 実行後、または `/tts_reload` で `config.yaml` を再読み込みし、Bot を再起動せずに反映します。

- 反映されるもの: `servers` の追加・変更・削除、`engines`、`defaults`、`ffmpeg_path`
- 削除されたサーバーは読み上げを止めて VC から退出します
- Twitch の受信チャンネルが変わったときだけ IRC を再接続します
- `discord.token` と `log_level` の変更は再起動が必要です
- 再読み込みに失敗した場合は、それまでの設定のまま動き続けます

## 設定リファレンス

全項目の例は [`config.example.yaml`](config.example.yaml) を参照してください。

### トップレベル

| キー | 既定値 | 説明 |
| :--- | :--- | :--- |
| `discord.token` | 環境変数 `DISCORD_TOKEN` | Bot トークン |
| `ffmpeg_path` | `ffmpeg` | ffmpeg の実行ファイル |
| `log_level` | `INFO` | `DEBUG` にすると読み上げ文もログに出る |
| `engines` | （必須） | 音声合成エンジンの定義 |
| `defaults` | | 全サーバー共通の `voice` / `reading` 既定値 |
| `servers` | 空 | Discord サーバーごとの設定（`/tts_setting` で追加可） |

### `engines.<名前>`

| キー | 説明 |
| :--- | :--- |
| `type` | `voicevox`（VOICEVOX 互換 API）または `coeiroink`（COEIROINK v2） |
| `url` | エンジンの URL。VOICEVOX は既定で `:50021`、COEIROINK v2 は `:50032`、COEIROINK v1 は `:50031`（`type: voicevox` で接続） |
| `timeout` | 合成リクエストのタイムアウト秒（既定 30） |

### `servers`

2 通りの書き方ができます。

```yaml
# マッピング形式（推奨）: サーバーID をキーにする
servers:
  111111111111111111: streamer_a
  222222222222222222:
    twitch: [streamer_b, streamer_c]
    voice_channel_id: 333333333333333333

# リスト形式
servers:
  - guild_id: 111111111111111111
    twitch: streamer_a
```

| キー | 説明 |
| :--- | :--- |
| `twitch` | Twitch チャンネル名（ログイン名）または URL。文字列またはリスト |
| `voice_channel_id` | 起動時に自動参加する VC の ID（任意） |
| `voice` | このサーバーの声（`defaults.voice` を上書き） |
| `reading` | このサーバーの読み上げ設定（`defaults.reading` を上書き） |
| `user_voices` | `Twitchログイン名: voice設定` で、ユーザーごとに声を変える |

### `voice`

| キー | 既定値 | 説明 |
| :--- | :--- | :--- |
| `engine` | 最初に定義したエンジン | `engines` のキー |
| `speaker` | `1` | VOICEVOX 互換エンジンの話者（スタイル）ID |
| `speaker_uuid` | | COEIROINK v2 の話者 UUID（COEIROINK では必須） |
| `style_id` | `0` | COEIROINK v2 のスタイル ID |
| `speed` / `pitch` / `intonation` / `volume` | `1.0` / `0.0` / `1.0` / `1.0` | 話速・音高・抑揚・音量 |

### `reading`

| キー | 既定値 | 説明 |
| :--- | :--- | :--- |
| `format` | `{name}、{message}` | 読み上げ書式 |
| `read_name` | `true` | `false` で本文のみ |
| `max_length` | `80` | 本文の最大文字数（`0` で無制限） |
| `truncate_suffix` | `、以下略` | 省略時に付ける文字列 |
| `url_replacement` | `URL省略` | URL の置換文字列 |
| `strip_emotes` | `true` | Twitch エモートを読まない |
| `ignore_users` | `[]` | 読まない Twitch ユーザー |
| `ignore_prefixes` | `["!"]` | このプレフィックスで始まるコメントは読まない |
| `dictionary` | `{}` | 読み替え辞書（長い語から順に置換） |
| `max_queue` | `20` | 未再生コメントの上限（超過分は破棄） |

## エンジンを別PCで動かす場合

Bot とエンジンは同じサブPCで動かすのが簡単ですが、別PCのエンジンを使うこともできます。

- VOICEVOX ENGINE は既定で localhost のみ受け付けるため、`run.exe --host 0.0.0.0` で起動し、`url` に `http://<エンジンPCのIP>:50021` を指定
- ファイアウォールで該当ポートを LAN 内に限定して開放

## ファイル構成

```
main.py                 エントリーポイント（Bot 起動 / 話者一覧）
start.bat               Windows 用起動スクリプト
config.example.yaml     設定サンプル
tts_bot/
  config.py             YAML 読み込み・検証
  config_writer.py      config.yaml へのサーバー設定書き込み（コメント保持）
  twitch_irc.py         Twitch チャット受信（匿名 IRC）
  text_filter.py        読み上げテキスト整形
  tts_engines.py        VOICEVOX / COEIROINK API クライアント
  guild_speaker.py      サーバーごとの読み上げキューと再生
  bot.py                Discord Bot 本体・ホットリロード
  commands.py           スラッシュコマンド
```

## トラブルシューティング

| 症状 | 対処 |
| :--- | :--- |
| `No module named 'aiohttp'` など | 依存パッケージ未インストール。`start.bat` で起動するか、`.venv\Scripts\python.exe -m pip install -r requirements.txt` を実行 |
| `/tts` コマンドが出ない | Bot 招待時に `applications.commands` スコープを付けたか確認。Discord クライアントを再起動（Ctrl+R）すると出ることがあります |
| `/tts_setting` が出ない | 「サーバー管理」権限が必要です |
| `/tts_setting` で保存失敗 | Bot の実行ユーザーが `config.yaml` に書き込めるか、YAML が壊れていないか確認 |
| VC に入るが無音 | ffmpeg のパス、エンジンの起動状態（起動ログの「接続OK」）を確認 |
| `エンジン ... に接続できません` | エンジンの起動・URL・ポートを確認。後からエンジンを起動しても読み上げ時に再接続します |
| COEIROINK で合成失敗 | `type: coeiroink` は v2 用。v1 の場合は `type: voicevox` + `:50031` |

## クレジット

音声合成エンジンの利用規約に従い、配信等で使用する場合は各エンジン・キャラクターのクレジット表記を行ってください（例: `VOICEVOX:ずんだもん`）。

## License

[LICENSE](LICENSE) を参照してください。
