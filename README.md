# Jev Coding Agent Harness

Jevによるコンテキスト選別を、ターミナルで観察するPython製のPoCです。
ファイル候補の **PIN / FULL / EXCERPT / DROP**、選別前後の推定サイズ、API失敗を表示します。

**現段階は `search + context-pack + metrics` の実装です。** Codexの内部会話履歴を変更したり、実際のCodexトークン消費を計測したりするものではありません。

## すぐに動かす

Python 3.11以上、Git。外部Pythonライブラリは不要です。

```sh
python -m jc demo --output /tmp/jev-demo-01
python -m jc replay /tmp/jev-demo-01
```

デモは固定スコアによる演出です。APIは呼びません。TTYでは色と縮むバーを表示し、リダイレクト時は通常のテキストになります。`--plain` または `NO_COLOR=1` で演出を無効化できます。

CLIとしてインストールする場合：

```sh
python -m pip install -e .
jc demo
```

## 実際のリポジトリを選別する

`TYPESAFE_API_KEY` を環境変数で設定してから実行します。キーはファイルやGitに保存しないでください。

```sh
python -m jc search 'refresh token が期限切れの際の500を修正' \
  --repo /absolute/path/to/repository \
  --budget 8000 --candidates 40 \
  --output /tmp/jev-live-01
```

このコマンドは候補ソースの抜粋をTypeSafe APIへ送信します。送信可能なリポジトリで使用してください。隠しパス、鍵らしいファイル、一般的な秘密情報のパターンは除外しますが、完全な秘密情報検出ではありません。

1. `git ls-files` で追跡済みのテキストソースを列挙。
2. パスと内容を単語一致で順位付け。60行単位で最大40候補。
3. 3候補ずつJev `/v1/systemone` の `noul` で関連性を判定。
4. 0.8以上はFULL、0.4以上はEXCERPT、それ以外はDROP。
5. 推定予算内に収めてcontext packと判定記録を保存。

EXCERPTは最初のタスク語一致の周辺行をそのまま切り出します。生成要約ではありません。行番号と候補IDから出所を確認できます。

APIのスコアは関連性のモデル出力であり、正解の保証や校正済みconfidenceではありません。固定閾値は検証用の初期値です。

## 保存するもの

| ファイル | 内容 |
|---|---|
| `context-pack.md` | 選別したソースとタスク |
| `decisions.json` | 全候補の判定、スコア、行番号、削減理由 |
| `metrics.json` | 推定サイズ、API呼出数、返却されたusage、処理時間 |
| `chunks.json` | 選別前の候補本文。除外候補も復元可能 |
| `events.jsonl` | 判定イベント。外部ビューアにも利用可能 |

出力先は新規ディレクトリのみで、既存の実行結果は上書きしません。画面表示はstderr、context packの絶対パスはstdoutに出力します。

```sh
python -m jc read /tmp/jev-live-01 CHUNK_ID
python -m jc replay /tmp/jev-live-01
```

`read` は保存済み候補の完全な本文を返します。候補生成段階で含まれなかったファイルは通常の `rg` やファイル読み取りで探索してください。フィルタはアクセス制御ではありません。

## Codexとの使い方

出力された `context-pack.md` の絶対パスを、Codexへ依頼するタスクに添えてください。
例えば「このcontext packを読んで修正してください。不足があれば通常のリポジトリ探索も行ってください」と指定します。

これは手動で連携するPoCであり、Codexを自動起動するwrapperやMCPではありません。`AGENTS.md` の自動読込やCodexのcompactionを置換しません。

## 失敗時・計測上の扱い

- 適格な追跡済み `AGENTS.md` / `AGENTS.override.md` はJevへ判定させずPIN。予算に収まらなければ失敗し、黙って削りません。Codex側の通常の指示探索は引き続き必要です。
- APIエラー、タイムアウト、不正・欠落回答は該当バッチをFULL候補に戻し、予算だけ適用。画面と記録にfallbackを残します。
- APIキー未設定はエラー。デモに自動で切り替えることはありません。
- 予算は `ceil(UTF-8 bytes / 3)` による推定値。実際のトークン上限や請求額の保証ではありません。
- 削減率は「取得した候補のソース量」と「残したソース量」の比較です。リポジトリ全体や通常Codex実行とのA/B測定ではありません。
- `codex_input_tokens` と `task_success` は未計測のため `null`。JevのusageはAPI返却値をバッチごとに保存。
- 日本語タスクから英語識別子への意味検索、AST解析、キャッシュ、自動Codex実行は未実装。
- ソースは1ファイル100 KB、1チャンク12 KBまで。大きいファイル、symlink、未追跡ファイルは対象外。
- context packとchunksにはソース本文が入るため、実行結果はリポジトリにコミットしないでください。

## テスト

```sh
python -m unittest discover -s tests -v
```

ネットワーク不要。選別、指示保持、予算、回答検証、失敗時fallback、symlink/秘密情報の除外、デモと再生を検証します。実API接続はAPIキーのある環境で別途確認が必要です。

## 着想

[tamaratran/fast-jev-compaction](https://github.com/tamaratran/fast-jev-compaction) の「残す内容はそのまま、不要な内容だけ落とす」という発想と可視化に着想を得ました。元リポジトリのREADME、`src/request.ts`、`src/types.ts` でAPIの入出力形式を確認しています。

元プロジェクトはClaude Codeの会話履歴を対象にしますが、このPoCはリポジトリのソース候補を対象にします。元のSwiftUIデモやTypeScript実装は同梱していません。
