# GiveAwayBot

Discordコミュニティ向けのGiveaway・チケット・Minecraft認証・モデレーションBOT。Python / discord.py / SQLite、Ubuntu 24.04・Dockerを想定しています。ISC Licenseで公開しています。

## 機能

- Giveaway / VIP Giveaway、ロール条件・小数倍率、抽選、当選DM、受け取り期限とチケット
- Java / BedrockのMinecraft本人確認、サーバーごとのID紐づけ・管理者による登録/解除
- Skelly売買、Buy/Sell個別受付、寄付、VIP申請・料金表・特典
- 警告・注意・タイムアウト・Kick・Ban・取り消し・異議申し立て・WarnStats・警告ロール復元
- Discord監査ログから外部処分を記録、メッセージ編集/削除ログ
- 禁止ワード、表記ゆれ、管理者が承認するAI関連表現候補
- チケットAI補助、OpenAIキー交換プログラム、別BOTとのチケット連携
- 英語のお知らせの日本語翻訳、Server Info表示
- Community構成・パネル・設定の一括設置（運営別テンプレートの準備が必要）

## 起動

1. Discord Developer Portalで自分のBOTを作成し、**Server Members Intent**を有効化してください。本文を扱う機能には**Message Content Intent**も必要です。
2. `.env.example` を `.env` にコピーし、`DISCORD_TOKEN` を自分の値に変更します。`.env` は公開しないでください。
3. DockerとDocker Compose v2を導入済みなら、次を実行します。

```sh
cp .env.example .env
chmod 600 .env
# .env を編集してから
 docker compose up -d --build bot
 docker compose logs --tail=50 bot
```

Ubuntu 24.04では `sudo bash setup.sh` でもDocker導入とBOT起動を行えます。既存の.envがなければトークンを非表示で入力します。

BOTを `bot`・`applications.commands` のスコープで招待し、利用する機能に必要な権限とロール順位を設定します。コマンドは起動時に同期されます。各機能のログ先・担当ロール・受付先はサーバーごとのコマンドで設定してください。全機能が初回から自動で有効になるわけではありません。

`/clear count:50` で実行チャンネルの直近1～100件を削除できます。実行者とBOTに閲覧・履歴・メッセージ管理権限が必要です。BOT投稿・ピン留めも含み、古い投稿は個別に削除します。結果は実行者だけに表示されます。

## データと外部サービス

永続データはSQLiteです。Dockerの `giveaway-data` ボリュームをバックアップしてください。`docker compose down -v` はデータを削除するため使用しないでください。

AIはオプションで、利用者自身のOpenAI APIキーと課金が必要です。チケットAIは指定メッセージ本文、自動翻訳は対象本文・埋め込み、禁止ワード候補生成は登録ワードをOpenAIへ送ります。APIリクエストは `store:false` です。通常の禁止ワード照合はローカルで行います。

Minecraft認証は `.env` に公開ホスト、ポート、共有シークレットを設定し、`docker compose --profile minecraft up -d --build` で起動します。JavaはTCP 25565、BedrockはUDP/TCP 19132とUDP 19133を使用します。ネットワーク環境・ゲーム更新によって接続確認が必要です。Bedrockのビルドにはgo.mod指定のGoが必要です。パスワードを取得する機能ではありませんが、Discord ID・UUID/XUID・名前・認証日時などを扱います。詳細は [PRIVACY.md](PRIVACY.md) を参照してください。

## 本番版との違い

2026-11-20時点の全Python機能、Java/Bedrock認証サーバー、テストを収録しています。既存の稼働BOTを操作するための公開リポジトリではありません。

- 本番トークン、APIキー、秘密鍵、DB、ログ、ユーザー情報、サーバーの実設定、Community原本スナップショットは含みません。
- 固有のDiscord IDは一貫したサンプルIDへ置換しています。`COMMUNITY_SOURCE_GUILD_ID`、`PEER_BOT_ID`を設定可能にしています。
- `community_setup.py` のチャンネル/ロール参照はサンプルです。一括設置を使用する場合は自分のテンプレートと対応する参照を準備してください。無設定ではこのコマンドを使用できません。他の一般機能は独立して動作します。
- `access_policy.py` の運営別制限、`moderation.py` のログ配送SQLにもサンプルIDがあります。自分の運営方針に合わせて設定してください。
- 別BOT連携・キー交換用スクリプトを収録していますが、運営環境のSSH鍵・systemd・移行専用RPCラッパーは含みません。定期実行や共有ディレクトリは各管理者が構成してください。Admin APIキーはBOTプロセスと分離します。
- 旧Node Bedrock実装は現行構成では使わず、`bedrock-go`が現行実装です。

## テスト

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python -m unittest discover -s tests
cd verifier && npm ci && npm test
# bedrock-go ディレクトリでは go test ./...
```

ユニットテスト合格はDiscord上のすべての操作・Minecraftの全クライアントでの接続を保証しません。導入先で検証してください。

## ライセンス・問い合わせ

[ISC License](LICENSE)。依存ライブラリにはそれぞれのライセンスが適用されます。Discord・Minecraft・DonutSMPの公式サービスではありません。

不具合・改善案はGitHub Issuesへ。秘密情報や個人のログを添付しないでください。セキュリティ報告は [SECURITY.md](SECURITY.md) を参照してください。
