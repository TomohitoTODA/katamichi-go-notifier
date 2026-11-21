# 片道GO! 新着通知

[トヨタレンタカー「片道GO!」](https://cp.toyota.jp/rentacar/)のご利用可能車種一覧を30分おきに確認し、
指定したルート（出発エリア→返却エリア）の**新しく掲載された車**をスマホの [ntfy](https://ntfy.sh/) アプリに通知します。

通知には出発店舗・返却先・期間・車種・車両条件・予約電話番号が入り、タップすると電話をかけられます。

## しくみ

- GitHub Actions が日本時間 6:00〜23:30 の30分おきに `notifier.py` を実行
- 一覧の「出発」タブの全車両を読み取り、`state/seen.json`（既読リスト）と比べて新着を検出
- 条件に合う新着だけを ntfy に送信（1回に9台以上あれば、8台を個別通知＋残りをまとめて1通）
- 一覧から消えた車は既読リストからも消すので、再掲載されたらまた通知されます
- 一覧が0件になった／ページ構造が変わった場合は、1回だけ警告を通知します

## 設定（GitHub の Settings → Secrets and variables → Actions）

| 種類 | 名前 | 内容 |
|---|---|---|
| Secret | `NTFY_TOPIC` | ntfy のトピック名（推測されにくいランダムな文字列） |
| Secret | `NTFY_TOKEN` | 任意。ntfy.sh のアクセストークンを使う場合のみ |
| Variable | `ROUTES` | 監視ルート。例：`関東>中部, 近畿>*`（未設定なら config.json） |

エリア名：北海道 / 東北 / 関東 / 中部 / 近畿 / 中国 / 四国 / 九州、`*` は「どこでも」。

## スマホ側

1. App Store / Google Play で「ntfy」をインストール
2. ＋ボタン →「Subscribe to topic」→ `NTFY_TOPIC` と同じトピック名を入力
3. GitHub の Actions タブ →「片道GO! 新着チェック」→ Run workflow で「テスト通知だけ送る」にチェックして実行 → 通知が届けば完了

## 手元での確認

```sh
pip install -r requirements.txt pytest
pytest -q tests
python notifier.py --dry-run          # 通知せず結果を表示
```
