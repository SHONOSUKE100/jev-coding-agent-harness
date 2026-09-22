# Jev Coding Agent Harness

Codexの前にJevによるコンテキスト選別を置き、その判断・使用量・実行経過をターミナルで観察するハーネスです。Python 3.11+、Git、Pythonの外部依存なし。

`jc exec "タスク"` で **候補取得 → Jev選別 → context pack → codex exec → 使用量記録** を実行します。既存のCodexをforkせず、通常の認証・AGENTS.md・権限設定を尊重します。

## インストールと最短の確認

```sh
python3 -m pip install -e .
jc --version
jc demo
```

`demo` は固定スコアの演出です。JevもCodexも呼びません。色付きの `PIN / FULL / EXCERPT / DROP` と、縮むコンテキストバーを表示します。`--plain` または `NO_COLOR=1` で通常のテキストにできます。

実リポジトリで、ネットワークもCodex起動もせずに配線を確認：

```sh
jc exec '認証処理を確認して' --repo /absolute/path/to/repo \
  --offline --dry-run --output /tmp/jev-dry-run-01
```

`--offline` はJevを呼ばず、候補を予算内で保持します。意味的な選別を模擬したスコアは生成しません。`--dry-run` 単独ではCodexだけを停止し、Jev選別は実行します。

## Codexで実行

Codex CLIをインストール・ログイン済みにし、`TYPESAFE_API_KEY` を環境変数で設定します。APIキーは設定ファイルやコマンド履歴に残さず、環境の秘密情報管理機能を利用してください。

```sh
jc doctor --repo /absolute/path/to/repo

# 調査：read-onlyがデフォルト
jc exec 'refresh tokenの期限切れ処理を調べて' --repo /absolute/path/to/repo

# 修正を許可する場合
jc exec '期限切れrefresh tokenが500になる不具合を修正して' \
  --repo /absolute/path/to/repo --sandbox workspace-write \
  --timeout 900 --output /tmp/jev-fix-01
```

`--codex /absolute/path/to/codex`、`--model MODEL` を指定できます。実行ファイルはシェル文字列ではなく単一パスです。`danger-full-access` や承認回避フラグは渡しません。既存のユーザー設定や指示を無効化しません。

CodexのJSONLイベントを随時表示し、終了、失敗、タイムアウト、キャンセルを記録します。正常終了かつ完了イベントありを「実行完了」としますが、タスクの正解・受入れとは別扱いです。

同じコマンドと出力の組合せが3回続くとループの疑いを記録します。任意の `--max-repeat 5` で5回時点の停止も指定できます。タイムアウト・停止時には子プロセス群を終了します。

## 追加の検索・読み取り

```sh
jc search 'refresh token validation' --repo /absolute/path/to/repo
jc search 'FooDefinition' --repo /absolute/path/to/repo --expand --candidates 100
jc symbol 'refresh' --repo /absolute/path/to/repo
jc read /path/to/run CHUNK_ID
jc replay /path/to/run
```

`jc-search`、`jc-read`、`jc-symbol` も同じコマンドの短縮名としてインストールされます。

- Git追跡済みファイルを走査し、単語・パス一致と未コミットの変更を使って候補を順位付け。
- PythonはASTによる関数・クラス境界を利用。大きい定義は80行単位。他言語は行単位で分割。
- `symbol` はPythonのAST、他言語の一部の宣言パターンを使う有界検索。
- 認証・期限・DBなどの少数の日本語語彙は英語の検索語に展開。埋め込み検索ではありません。
- Jevは0〜1の関連性を返し、初期値では0.8以上をFULL、0.4以上をEXCERPT、それ以外をDROP。
- EXCERPTは該当語の周辺行をそのまま抜粋。生成要約は使いません。
- `read` は元候補を復元するため、DROP済みの候補も読めます。候補に入らないファイルには通常の `rg` 等で戻れます。

Codexへ渡すプロンプトには追加検索の実行方法も入ります。実行中の子 `jc search` は親の実行記録にリンクされ、Jev使用量を集約します。

## 設定・補助ルール

```sh
jc init --repo /absolute/path/to/repo
```

新規の `.jev/` に設定と補助ルールの雛形を作ります。既存ファイルは上書きせず、`AGENTS.md` やCodexのグローバル設定も変更しません。

`.jev/config.toml` の主な設定：

```toml
budget = 8000
candidates = 40
full_threshold = 0.8
excerpt_threshold = 0.4
model = "jev-latest"
timeout = 20
retries = 1
cache_ttl = 86400
rules_manifest = ".jev/rules.toml"
tools_catalog = ".jev/tools.json"
tool_limit = 5
routing_threshold = 0.95
```

`--config /path/to/config.toml` で別設定を選び、`--budget` / `--candidates` で上書きできます。未知のキー、不正な型、範囲外の値は拒否します。設定からシェルコマンドは実行しません。

補助ルールのmanifest：

```toml
[[rules]]
path = ".jev/rules/base.md"

[[rules]]
path = ".jev/rules/backend.md"
optional = true
```

`optional = true` と明示した補助ルールだけがJevの選択対象です。それ以外は必ず含めます。API失敗時は任意ルールも保持します。補助ルールは要約せず、予算不足なら失敗します。

```sh
jc rules 'DB migrationを修正' --repo /path/to/repo
```

**AGENTS.mdはこのルーターの対象にできません。** 適格な追跡済みAGENTSファイルはPINし、読み込めない・予算を超える場合は黙って落とさず失敗します。ファイル内のスコープや優先順位はCodexの通常の指示解決に従います。

## キャッシュとAPI失敗時

SQLiteへ **タスク全文・モデル名・質問形式・バッチ内全候補の本文** を含むキーで保存します。同じカテゴリという理由で別タスクの判定を再利用しません。本文変更、質問変更、TTL切れで再判定します。

`--no-cache` で無効化できます。APIキーがなくても全件キャッシュにヒットする場合は実行できますが、未キャッシュの判定はエラーです。

HTTPタイムアウト・一時的な障害は指定回数だけ再試行します。401/403等は再試行しません。失敗した候補はFULL候補へ戻し、予算による制限だけ適用します。ログにはAPIレスポンス本文やキーを出しません。

## MCPから利用

ローカルCodex CLIへ手動登録できます。先に上記のインストールを済ませてください。

```sh
codex mcp add jev-harness -- /absolute/path/to/jc serve --repo /absolute/path/to/repo
```

APIキーなしの確認では末尾に `--offline` を付けます。実環境ではMCPプロセスへ `TYPESAFE_API_KEY` が渡るよう、Codex設定の `env_vars` を利用してください。API待ち時間を考慮して `tool_timeout_sec` を設定できます。

提供ツール：

| ツール | 内容 |
|---|---|
| `jc_search` | 選別済みcontextと全候補IDを返す |
| `jc_read` | 同じサーバーセッションで生成した候補本文を読む |
| `jc_symbol` | シンボル名を検索 |
| `jc_tools` | 外部ツールカタログから関連スキーマを発見 |

stdioのJSON-RPCです。HTTPサーバー、任意コマンド実行、接続先リポジトリのリクエストごとの変更には対応しません。サーバー内は逐次処理で、実行中APIリクエストのMCPキャンセルには未対応です。各API要求にはタイムアウトがあります。起動時に指定したリポジトリに固定します。

## ツールスキーマ選択

`examples/tools.json` のような `{name, description, inputSchema}` の配列をカタログに設定します。

```sh
jc tools 'database migrationを調べたい' --repo /path/to/repo
jc tools 'database migrationを調べたい' --repo /path/to/repo --expand
```

Jevに渡すのは名前・説明だけで、選ばれたスキーマを返します。`--expand` は全件を返す逃げ道です。外部ツール自体は実行せず、接続・認可は別途必要です。Codexに設定済みの他のMCPサーバーを勝手に無効化したり、グローバルなツール一覧を差し替えたりしません。

## 任意のモデル選択

利用可能な実際のモデル名を設定してください。特定のモデル名・価格を固定していません。

```toml
small_model = "YOUR_SMALL_MODEL"
large_model = "YOUR_LARGE_MODEL"
routing_threshold = 0.95
```

```sh
jc route-model '局所的な名称変更' --repo /path/to/repo
jc exec '局所的な名称変更' --repo /path/to/repo --auto-model
```

「局所的・機械的で、設計やセキュリティ判断、未知のデバッグを必要としない」という条件をJevで判定します。閾値未満やAPI失敗時はlarge_modelを選びます。実行開始時に一度だけ選び、途中で会話を別モデルへ移しません。スコアは校正済みの信頼度ではなく、初期設定のヒューリスティックです。`--model` との併用は不可です。

## ログ・使用量・評価

```sh
jc log /path/to/test.log
jc evaluate /path/to/run --success true --note '関連テストを実行し、要求を満たすことをレビュー済み'
```

`log` は依存関係・DB・ネットワーク・型・テスト失敗のパターンを抽出し、行番号付きで返します。決定的な分類で、APIへログを送りません。

Codexの `turn.completed.usage` をターンごとに集計します。取得していない数字は0にせず `null` とし、終了コード0だけではタスク成功にしません。`evaluate` は人による受入れ記録です。

A/B比較：

```sh
jc exec '調査タスク' --repo /path/to/repo --baseline --model YOUR_MODEL --output /tmp/jev-base-01
jc exec '調査タスク' --repo /path/to/repo --model YOUR_MODEL --output /tmp/jev-filtered-01
jc compare --baseline /tmp/jev-base-01 --harness /tmp/jev-filtered-01
```

タスク、コミット、作業ツリー差分、モデル指定、sandboxが一致しない比較は拒否します。修正タスクの公平な比較には、次の `bench` を使ってください。

```sh
jc bench examples/tasks.json --repo /path/to/clean/repo \
  --model YOUR_MODEL --sandbox workspace-write --output /tmp/jev-benchmark-01
```

タスクごとにbaselineとharnessを**別々の新規worktree**で動かします。クリーンなリポジトリが必要です。各作業ツリーは差分レビュー用に残し、破棄しません。依存関係インストールやテストをハーネス側で勝手に実行しません。`benchmark.json` に全実行先が記録されます。

各runをレビューして `evaluate` 後、複数パスを `compare --baseline ... --harness ...` に渡します。「成功タスクあたり入力トークン」は失敗したタスクの使用量も分子に含めます。未評価のタスクがある場合は成功率を算出しません。モデル指定を省略した場合、外部のCodex設定変更まで同一性を保証できません。

価格も比較したい場合、次のキーを持つJSONを用意し、`compare --prices /path/to/prices.json` を使います。値はすべて **USD / 100万トークン** です。

`codex_input`, `codex_cached_input`, `codex_output`, `jev_input`, `jev_output`

価格の初期値はありません。使用量欠損やAPI失敗があれば料金は `null` です。計算対象はこのハーネスで計測したCodex・Jevのみで、他社ツールの課金や未リンクの外部実行は含みません。

## 保存場所と成果物

指定がなければ対象リポジトリのGit管理領域内 `jev-harness/runs/<id>/` に保存します。キャッシュも同じ管理領域内です。ソースリポジトリへ実行ログをコミットしません。`--output` を指定する場合は新規ディレクトリのみを使用します。

| ファイル | 内容 |
|---|---|
| `context-pack.md`, `chunks.json` | 選別後と選別前の候補 |
| `decisions.json`, `events.jsonl` | 判断・根拠・サイズ |
| `rules.json`, `current-rules.md` | 補助ルールの選択と本文 |
| `tools.json` | 選択したツールスキーマ |
| `prompt.md`, `command.json` | 実際の入力と起動引数 |
| `codex-events.jsonl`, `codex-stderr.log` | Codex実行ログ |
| `final-message.md` | Codexの最終メッセージ |
| `metrics.json` | 実測usage・推定サイズ・実行状態・受入れ |
| `child-searches.jsonl` | 実行中の追加検索との対応 |
| `model-routing.json` | モデル選択を有効にした場合の記録 |

ソース・タスク・ツール出力を含みます。生成ファイルを公開リポジトリへ追加しないでください。既知のAPIキーはCodexの保存ログから伏せますが、あらゆる秘密情報を検出する機能ではありません。

## 対応範囲と制限

- 実Jev APIとインストール済みCodexの接続確認は、各利用環境で必要です。自動テストは偽のAPI応答と実行ファイルを使用します。
- 推定予算は `ceil(UTF-8 bytes / 3)`。実トークナイザーによる上限保証やCodexの料金削減保証ではありません。
- 通常探索のフィルタをすべて強制的に経由させる機能や、Codex内部のcompaction置換はありません。
- Git追跡済み・許可拡張子・最大100KBのファイル、最大12KBのチャンクを対象にします。隠しパス・symlink・binary・秘密らしい内容は除外します。完全な秘密情報検出ではありません。
- `--expand` も有界候補検索です。必要なら通常の検索へ戻ってください。
- API入力には候補ソース本文が含まれます。送信可能なリポジトリで利用してください。
- 主な実行対象はmacOS/Linuxです。実行ごとのディレクトリを分離し、必須ルールを共有のcurrent-rulesファイルへ書き換える方式は採りません。

## 開発・テスト

```sh
python -m unittest discover -s tests -v
```

APIキー不要。設定検証、AST検索、キャッシュ無効化、API失敗、予算とルール保持、Codexサブプロセスへのstdin、JSONL使用量、タイムアウト、ループ停止、料金の欠損処理、MCP stdio、独立worktreeのA/B試行を検証します。CIはPython 3.11〜3.13 / Ubuntu・macOSで実行します。

## 参考

- [fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction)：可視化と、残す情報を書き換えない設計の着想。元プロジェクトのソースは同梱していません。
- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)：`exec --json`、stdin、usageの接続仕様。
- [Codex MCP](https://developers.openai.com/codex/mcp)：stdioサーバー登録と設定。
- [MCP stdio](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、[Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)：通信とツール仕様。
