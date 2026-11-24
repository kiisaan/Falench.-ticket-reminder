# Falench. チケット発売リマインダー

LivePocket とチケットダイブから **Falench. 出演イベントの販売開始日時** を集めて、次の通知を LINE と Discord に送ります。

| タイミング | 内容 |
|---|---|
| 毎朝 6:45 以降の最初の起動 | 今日発売するチケットの一覧（先着・抽選の区別つき）＋翌日0:00〜6:59の発売分 → Discord、LINE（残り通数に余裕があれば） |
| 先着販売の1時間前 | 「あと1時間」 → Discord だけ |
| 先着販売の30分前 | 「あと30分」 → Discord、LINE（残り通数に余裕があれば） |
| 先着販売の5分前 | 「あと5分」 → **LINE（必ず送る）**と Discord（cron-job.org と GitHub の二重体制） |

抽選販売は朝の一覧に載せるだけで、直前のリマインドはしません。先着か抽選か判別できなかったものは、念のためリマインドします。

GitHub Actions（公開リポジトリなら無料）と cron-job.org（無料）で動きます。

## しくみ

- 1時間ごとに起動します。起動したときに **90分以内に送る通知があれば、その時刻までジョブの中で待ってから** 送ります。GitHub の起動が数分〜数十分遅れても、通知は時刻どおりに届きます。
- 販売情報は3時間ごとに取り直します（朝の一覧の前にも取り直します）。
- 送信済みの通知は `state.json` に記録して、同じ通知を二度送らないようにしています（ワークフローが自動でコミットします）。
- 起動が遅れて通知時刻を過ぎていた場合は、まとめて1通だけ送ります。発売時刻を過ぎていた場合は送りません。

### 5分前の通知を遅らせないための二重体制
1. 販売情報を集めたときに、5分前の通知を LINE・Discord それぞれ **cron-job.org に予約** します。cron-job.org が予定時刻ちょうどに LINE と Discord へ直接送るので、GitHub の混雑や遅れの影響を受けません。
2. GitHub 側も1時間前から待機していて、5分前の40秒後に cron-job.org の実行履歴を確認します。**届いていなければ、その場で GitHub から送ります。**
3. 発売時刻が変わったら予約を取り消して入れ直し、終わった予約は自動で消します。

`CRONJOB_API_KEY` を登録しなければ、GitHub だけで送ります（その場合も待機中なら時刻どおりに届きます）。

## セットアップ

### 1. リポジトリを作る
GitHub で **Public** のリポジトリ（例: `Falench.-ticket-reminder`）を新しく作り、このフォルダの中身をすべてアップロードします（`.github/workflows/reminder.yml` も含めます）。

### 2. Secrets を登録する
Settings → Secrets and variables → Actions → New repository secret

| 名前 | 中身 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | 今のbotと同じもの |
| `LINE_TO` | 送り先（グループIDかユーザーID）。未設定なら `LINE_GROUP_ID` → `LINE_USER_ID` の順に使います |
| `DISCORD_WEBHOOK_URL` | 今のbotと同じもの、またはリマインド用チャンネルのWebhook |
| `CRONJOB_API_KEY` | cron-job.org の Settings → API で作るAPIキー（5分前通知の二重化用） |

### 3. 書き込み権限を確認する
Settings → Actions → General → Workflow permissions を **Read and write permissions** にします（`state.json` を保存するため）。

### 4. テストする
Actions → 「Falench. Ticket Sale Reminder」→ Run workflow
- `test` にチェック → テスト通知が1通届くか確認
- `collect` と `dry_run` にチェック → 実際には送らずに、取れた販売開始日時をログで確認

### 5. cron-job.org から30分ごとに起動する（推奨）
今のbotと同じ方法で、cron-job.org から **毎時20分と50分** に workflow_dispatch を呼びます（起動の機会を増やすため）。

- URL: `https://api.github.com/repos/<ユーザー名>/<リポジトリ名>/actions/workflows/reminder.yml/dispatches`
- Method: POST
- Headers: `Authorization: Bearer <GitHubのトークン>` / `Accept: application/vnd.github+json`
- Body: `{"ref":"main"}`

（GitHub 側の毎時17分・47分の定期実行も保険として入っています）



## 設定の変更（config.json）

| 項目 | 意味 | 初期値 |
|---|---|---|
| `remind_offsets_minutes` | 何分前に通知するか | `[60, 30, 5]` |
| `morning_time` | 朝の一覧を送る時刻（この時刻以降の最初の起動で送る） | `"06:45"` |
| `morning_include_until_hour_next_day` | 朝の一覧に含める「翌日の何時まで」 | `7` |
| `morning_send_when_empty` | 発売がない日も「なし」と送るか | `false` |
| `remind_sale_types` | リマインドする販売方式（`first_come`=先着、`lottery`=抽選、`unknown`=不明） | `["first_come", "unknown"]` |
| `line_offsets_minutes` | LINEにも送るリマインド | `[30, 5]` |
| `line_protected_offsets_minutes` | 残り通数に関係なく必ずLINEに送るもの | `[5]` |
| `line_reserve_extra_sends` | 5分前用に確保する予備の回数 | `3` |
| `line_group_size` | LINEの人数（APIで取れなかったとき用） | `5` |
| `line_morning` | 朝の一覧をLINEにも送るか | `true` |
| `discord_mention` | 5分前の通知に付けるDiscordメンション（`""` で無し） | `""` |
| `cronjob_offsets_minutes` | cron-job.org に予約する通知（例: `[30, 5]`） | `[5]` |
| `collect_every_minutes` | 販売情報を取り直す間隔 | `180` |

主催者ページの追加は `organizers.json` に今のLivePocket版と同じ形で書きます。

## LINEの送信数について

LINE公式アカウントの無料プランは **月200通** で、グループに送ると人数分カウントされます（5人グループなら1回で5通）。

このbotは送るたびにLINEの残り通数を確認し、次のように使い分けます。

1. **5分前の通知は必ず送る**
2. 30分前と朝の一覧は、「今月これから送る5分前の通知（＋予備3回）」の分を残せるときだけLINEにも送る。足りなくなったらDiscordだけにする

目安（5人グループ、先着が月30件の場合）: 5分前だけで 30×5＝150通。残りの約50通を30分前と朝の一覧に使います。

**リマインド専用のLINE公式アカウントを新しく作るのがおすすめです。** 無料通数はアカウントごとなので、今の新着通知botとは別に200通を使えます。同じアカウントのままだと、新着通知で通数を使った月は30分前の通知がほとんど送れなくなります。新しく作ったアカウントをグループに招待して、そのアカウントのトークンとグループIDを Secrets に登録してください。

## うまく取れないとき

- Actions のログに `[TicketDive] JSONの販売開始キー: [...]` や `販売開始 ○ 件` が出ます。0件のイベントがあれば、そのURLを教えてください。
- 先着か抽選かは、券種名や「抽選受付期間」「先着」などの表記、チケットダイブのデータの中身から判定しています。判定違いがあれば、そのURLを教えてください。
- cron-job.org のAPIは1日100回までです。1回の発売で予約・確認・削除の3回使うので、1日30件程度までは問題ありません。
- 発売日だけで時刻が書かれていないチケットは、朝の一覧に「時刻はページで確認」として載せ、直前リマインドは送りません。
