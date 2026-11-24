# RESUME（続きの作業メモ）

## いまの状態（2026-11-24）

- junban_cpp（2fde60a）を土台にコピー。順番待ちの部分は外し、ログイン・テナントと 502 修正済みの DB 層を引き継いだ。
- 勤怠の MVP ができた（Python 版と C++ 版の両方）。
  - API: `python/testsv_api_kintai.py` / `cpp/main/testsv_api_kintai.h`（7 本）
  - モデル: `python/model_kintai.py` / `cpp/models/kintai.ddl` → `model_kintai.h`（ddl2cpp で生成）
  - ページ: `clock.html`（扉の QR で開く打刻）、`month.html`（月の集計・CSV）、`requests.html`（修正リクエスト）、
    `index.html` にお店の管理者向けの「扉に貼る打刻の QR」
- ローカル（SQLite）: `tools/kintai_check.py` の 56 手順の応答が Python と C++ で完全一致。
  3 ページを Chrome ヘッドレスで店員・管理者として描画して確認。
- TiDB: junban 用クラスタにデータベース `kintai` を作成済み（接続は password リポ secrets/tidb_junban.env の DB_NAME だけ kintai に）。

## ユーザーが決めた仕様

- 固定の QR、不正対策はしない。出勤・退勤だけ（休憩なし）。
- 修正リクエストは店員が出し、お店の管理者だけが承認。元の打刻と履歴は残す。
- 月単位の集計。データは 1 年で自動消去。

## 次にやること

1. Render に kintai-cpp / kintai-python を作る（Render API で。GitHub 連携が kintai_cpp を見られるかが条件）。
2. 本番で `tools/kintai_check.py <url> mysql` を両方に流して一致を確認。
3. 実機（スマホ）でパスキー登録 → お店作成 → 扉の QR → 打刻 → 修正リクエスト → 承認。

## あとでやるかもしれないこと

- お店ごとの固定の休憩（「6 時間を超えたら 45 分引く」など）
- 管理者が直接打刻を直す（リクエストを経ずに）
- 月の締め（締めた月は直せないようにする）

## ハマりどころ（junban_cpp から引き継ぎ）

- ポートは env `POST`（PORT ではない）。Render の PORT を橋渡ししている。
- C++ は TiDB のときスレッドごとに接続、SQLite のときは全体 mutex。Python は DB 処理をスレッドプールで。
- ページの定期更新は前の回が終わってから（`kintai.js` の `poll`）。
- `cpp/html` と `python/html` は同じ中身。片方を直したらもう片方へコピーする。
- SQLite のローカル C++ はデバッグ出力が多くて遅い（database.h の config.debug）。検査には支障なし。
