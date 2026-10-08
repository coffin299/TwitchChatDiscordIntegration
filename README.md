# TwitchChatDiscordIntegration

Twitch のチャットを **VOICEVOX / COEIROINK** で音声合成し、**Discord のボイスチャンネル**で読み上げる Bot です。

配信PCに負荷をかけないよう、**サブPCで Bot と音声合成エンジンを常駐**させる構成を想定しています。
`Discord ユーザーID : Twitch チャンネル` の対応を YAML で管理し、登録した人が **どのサーバーの VC でも** `/tts join` するだけでその人の配信コメントを読み上げます。サーバー ID の設定は不要で、1 つの Bot で複数の身内鯖を同時に扱えます。

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
- Discord ユーザー単位で Twitch を登録（サーバー ID 不要）
  - 複数サーバーで同時に別々の人の配信を読める / 同じ配信を複数サーバーで読むことも可
  - 1 人に複数 Twitch チャンネルを登録可（コラボ配信など）
- ユーザーごと・Twitch 視聴者ごとに話者・話速などを変更
- エモート除去、URL 省略、長文省略、読み替え辞書、無視ユーザー、コマンド（`!` 始まり）無視
- VC が無人になったら自動退出
- **`/tts_setting` で自分の Twitch を登録** → `config.yaml` に保存して即ホットリロード（再起動不要）
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

`config.yaml` を編集します。最小構成は以下だけです。`users` は空のままでも起動でき、後から各自が Discord 上の `/tts_setting` で登録できます。

```yaml
discord:
  token: ${DISCORD_TOKEN}      # 環境変数から読む（直書きも可）

engines:
  voicevox:
    type: voicevox
    url: http://127.0.0.1:50021

users:
  111111111111111111: streamer_a    # DiscordユーザーID: Twitchチャンネル名
  222222222222222222: https://www.twitch.tv/streamer_b   # URL でも可
```

Discord のユーザー ID は、Discord の「開発者モード」を ON にしてユーザーを右クリック →「ユーザーIDをコピー」で取得できます（`/tts_setting` を使えば ID を調べる必要はありません）。

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

1. 初回だけ `/tts_setting twitch:https://www.twitch.tv/自分のチャンネル` で自分の Discord アカウントと Twitch を紐付け
2. 配信するときに、どのサーバーでもいいので VC に入って `/tts join`
3. 自分の Twitch チャンネルのコメントがその VC で読み上げられます
4. 配信PCでは Discord でその VC に参加していれば OK（合成はサブPCが担当）

| コマンド | 権限 | 内容 |
| :--- | :--- | :--- |
| `/tts join [user] [twitch] [save]` | 全員 | 実行者がいる VC に参加し、`user`（省略時は自分）の Twitch を読み上げ開始。`twitch` で未登録でもその場で指定可 |
| `/tts leave` | 全員 | VC から退出し、待機中のコメントを破棄 |
| `/tts skip` | 全員 | 再生中の読み上げを止め、待機中も全て破棄 |
| `/tts status` | 全員 | 対象ユーザー・Twitch チャンネル・VC・エンジン・待機件数を表示 |
| `/tts_setting twitch [user]` | 全員 | 自分の Twitch を登録・変更し、`config.yaml` に保存してホットリロード |
| `/tts_reload` | サーバー管理 | `config.yaml` を手動編集した後に再読み込み |

- 1 サーバーで同時に読めるのは 1 人分です。別の人が `/tts join` すると、その人の配信に切り替わります
- 別々のサーバーなら、同時に別々の人の配信を読めます
- 他の人の配信を読みたいときは `/tts join user:@その人`（その人が登録済みであること）
- VC に Bot 以外が誰もいなくなったら自動で退出します（他の Bot は人数に数えません。無人の VC に移動させられた場合も退出）

### `/tts join` の一時指定（登録不要）

`twitch` を付けると、`config.yaml` に登録していなくてもその場で任意の Twitch チャンネルを読み上げます。

| 例 | 動作 |
| :--- | :--- |
| `/tts join` | 自分の登録済み Twitch を読む |
| `/tts join user:@A` | A の登録済み Twitch を読む |
| `/tts join twitch:https://www.twitch.tv/xxx` | xxx を一時的に読む（未登録でも可） |
| `/tts join user:@A twitch:xxx` | xxx を A の声・読み方設定で一時的に読む（A が未登録なら `defaults`） |
| `/tts join user:@A twitch:xxx save:True` | A ⇔ xxx を `config.yaml` に登録してから読む（他人は Bot オーナーのみ） |

- 一時指定は保存されません。`/tts leave` や Bot の再起動で消えます（`/tts_reload` では維持されます）
- 声・読み方は、`user` が登録済みならその人の `voice` / `reading` / `viewer_voices`、未登録なら `defaults` を使います
- VC に接続していない間のコメントは溜めずに捨てます（参加した瞬間に大量に読まれるのを防ぐため）

### `/tts_setting` の引数

| 引数 | 必須 | 内容 |
| :--- | :---: | :--- |
| `twitch` | ✅ | Twitch の URL（`https://www.twitch.tv/xxx`）またはチャンネル名。カンマ・空白区切りで複数可 |
| `user` | | 登録対象の Discord ユーザー。省略時は自分。**他人を登録できるのは Bot オーナーのみ** |

- 登録済みなら `twitch` だけを上書きし、`voice` などの個別設定は残します
- `config.yaml` のコメントは保持されます。保存前に検証し、不正なら書き込みません

### ホットリロード

`/tts_setting` 実行後、または `/tts_reload` で `config.yaml` を再読み込みし、Bot を再起動せずに反映します。

- 反映されるもの: `users` の追加・変更・削除、`engines`、`defaults`、`ffmpeg_path`
- 読み上げ中のユーザーの設定が変わった場合は、そのまま新しい設定で読み上げを続けます
- 削除されたユーザーの読み上げは止めて VC から退出します
- Twitch には読み上げ中のチャンネルだけ接続し、変化したときだけ IRC を再接続します
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
| `defaults` | | 全ユーザー共通の `voice` / `reading` 既定値（未登録ユーザーの一時指定でも使用） |
| `users` | 空 | Discord ユーザーごとの設定（`/tts_setting` で追加可） |

### `engines.<名前>`

| キー | 説明 |
| :--- | :--- |
| `type` | `voicevox`（VOICEVOX 互換 API）または `coeiroink`（COEIROINK v2） |
| `url` | エンジンの URL。VOICEVOX は既定で `:50021`、COEIROINK v2 は `:50032`、COEIROINK v1 は `:50031`（`type: voicevox` で接続） |
| `timeout` | 合成リクエストのタイムアウト秒（既定 30） |

### `users`

Discord ユーザー ID をキーにします。値は Twitch チャンネルだけの省略形か、詳細設定です。

```yaml
users:
  111111111111111111: streamer_a                  # 省略形
  222222222222222222: [streamer_b, streamer_c]    # 複数チャンネル
  333333333333333333:                             # 詳細設定
    twitch: https://www.twitch.tv/streamer_d
    voice:
      speaker: 8
    viewer_voices:
      friend_x: { speaker: 2 }
```

| キー | 説明 |
| :--- | :--- |
| `twitch` | Twitch チャンネル名（ログイン名）または URL。文字列またはリスト |
| `voice` | この人の配信を読むときの声（`defaults.voice` を上書き） |
| `reading` | この人の配信の読み上げ設定（`defaults.reading` を上書き） |
| `viewer_voices` | `Twitchログイン名: voice設定` で、視聴者ごとに声を変える |

旧形式の `servers`（サーバー ID 基準）は廃止しました。残っているとエラーになるので `users` に書き換えてください。

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
  config_writer.py      config.yaml へのユーザー登録書き込み（コメント保持）
  twitch_irc.py         Twitch チャット受信（匿名 IRC）
  text_filter.py        読み上げテキスト整形
  tts_engines.py        VOICEVOX / COEIROINK API クライアント
  guild_speaker.py      サーバーごとの読み上げセッション（キューと再生）
  bot.py                Discord Bot 本体・セッション管理・ホットリロード
  commands.py           スラッシュコマンド
```

## トラブルシューティング

| 症状 | 対処 |
| :--- | :--- |
| `No module named 'aiohttp'` など | 依存パッケージ未インストール。`start.bat` で起動するか、`.venv\Scripts\python.exe -m pip install -r requirements.txt` を実行 |
| `/tts` コマンドが出ない | Bot 招待時に `applications.commands` スコープを付けたか確認。Discord クライアントを再起動（Ctrl+R）すると出ることがあります |
| `Discord へのログインに失敗しました` / `401 Unauthorized` | トークンが不正です。Developer Portal → Bot → **Reset Token** で発行したものを `discord.token` に設定（Client Secret や Application ID ではありません）。環境変数 `DISCORD_TOKEN` を使う場合は値が正しいか確認 |
| `/tts join` で「未登録」と出る | `/tts join twitch:<URL>` で一時指定するか、`/tts_setting twitch:<URL>` で登録 |
| `servers は廃止しました` エラー | 旧形式の設定です。`servers:` を `users:` にし、キーを Discord ユーザー ID に変更 |
| `/tts_setting` で保存失敗 | Bot の実行ユーザーが `config.yaml` に書き込めるか、YAML が壊れていないか確認 |
| VC に入るが無音 | ffmpeg のパス、エンジンの起動状態（起動ログの「接続OK」）を確認 |
| `エンジン ... に接続できません` | エンジンの起動・URL・ポートを確認。後からエンジンを起動しても読み上げ時に再接続します |
| COEIROINK で合成失敗 | `type: coeiroink` は v2 用。v1 の場合は `type: voicevox` + `:50031` |
| `/v1/synthesis -> HTTP 500` | COEIROINK に `speaker_uuid` / `style_id` の話者が入っていない場合に多い。`--list-speakers` の値に直すか、`voice.engine: voicevox` に切り替え。起動時ログに「話者 ... がエンジン ... にありません」と出ます |

## クレジット

音声合成エンジンの利用規約に従い、配信等で使用する場合は各エンジン・キャラクターのクレジット表記を行ってください（例: `VOICEVOX:ずんだもん`）。

## License

[LICENSE](LICENSE) を参照してください。
