# kintai_cpp — QR で打刻する勤怠

店員がお店の扉に貼った QR を読んで「出勤」「退勤」を押すだけの勤怠アプリ。打刻の間違いは修正リクエストで
直し（お店の管理者が承認）、月ごとの勤務時間を集計する。**同じ API を FastAPI(Python) と cpp-httplib(C++)
の両方**で実装し、**Render.com**（無料枠）＋ **TiDB Cloud** で動かす。
土台は [yomei-o/junban_cpp](https://github.com/yomei-o/junban_cpp)（パスキーのログインとテナント）。
Python/C++ の**機能パリティ**を保つのが方針（速度は別）。

## 使い方

| 誰が | ページ | やること |
|---|---|---|
| お店の人 | `/html/index.html` | パスキーでログイン。全体の管理者がお店（テナント）を作り、URL を店員に渡す。店員はその URL から参加申請。お店の管理者の画面に**扉に貼る打刻の QR** が出る |
| 店員 | `/html/clock.html?tenant=<guid>`（扉の QR） | 「出勤」「退勤」を押す（次に押すほうが目立つ）。押した直後に「間違えた」から修正をお願いできる |
| 店員・管理者 | `/html/month.html?tenant=<guid>` | 月の勤務日数・合計時間と日ごとの明細。打刻漏れを目立たせ、そこから足す・直す・消すをお願いできる。管理者は全員分と CSV |
| 店員・管理者 | `/html/requests.html?tenant=<guid>` | 修正リクエストの一覧。管理者は承認・却下、本人は取り下げ |

- 打刻は**出勤・退勤だけ**（休憩は打刻せず、差し引かない）。不正（QR の写真で家から打刻など）は対策しない。
- 月の集計: 打刻を時刻順に並べ、出勤のあとの最初の退勤（24 時間以内）と組にする。0 時をまたいだ勤務は
  出勤した日（日本時間）の分。組にならない打刻は「打刻漏れ」。
- 打刻は書き換えない。修正を承認すると元の打刻に「消した」印を付け、直した打刻を新しく足す（履歴が残る）。
- 打刻と修正リクエストは **1 年（365 日）で自動で消す**（打刻・状況の API の中で 1 時間に 1 回）。

## API（`/api/kintai/v0/`、すべて POST・JSON、ログインしたそのお店の承認済みメンバー）

| API | 引数 | 備考 |
|---|---|---|
| `status` | `tenant` | 自分の最後の打刻と次に押すほう |
| `punch` | `tenant, kind`（in / out） | |
| `month` | `tenant, month`（"YYYY-MM"）`[, user \| all]` | 他の人・全員はお店の管理者だけ |
| `request` | `tenant, action`（change / add / delete）`, punch, kind, at, reason` | |
| `requests` | `tenant[, mine]` | 管理者は全員分 |
| `request_cancel` | `tenant, id` | 自分の承認待ちだけ |
| `request_decide` | `tenant, id, approve` | お店の管理者だけ |

時刻は API ではすべて UTC の `"YYYY-MM-DD HH:MM:SS"`（日本時間への換算はページ側）。
詳しくは `python/testsv_api_kintai.py` と `cpp/main/testsv_api_kintai.h` の先頭。

### 両実装が同じ応答を返すかの検査

`tools/kintai_check.py` はサーバに同じ手順（56 手順）を流し、応答を正規化して出す。集計を確かめるため、
前の月の決まった時刻の打刻（普通の勤務・0 時またぎ・退勤漏れ・出勤漏れ・月またぎ・消した打刻）を DB に直接入れる。
Python 版と C++ 版の出力を `diff` して一致を確かめる。DB に `mysql` を渡すと本番の TiDB で流し、最後に消す。

## 構成

```
render.yaml       Render Blueprint（kintai-python と kintai-cpp の2サービス）
cpp/              cpp-httplib 実装（httplib + 自作mbedtls + sqlpp11 + sqlite3 同梱）
  main/           lambda_server.cpp（エントリ）, API(testsv_api_*), httplib/json
  include/        mbedtls（mbedtlspp 経由でユニティ include）
  sqlite/ sqlpp11/ mysql_8_4/ postgre_17/ models/ gen/ date/
  html/           index.html（ログイン）, clock/month/requests.html（勤怠）, kintai.js/css, qr.js/qr.wasm
                  （python/html と同じ中身。片方を直したらコピーする）
  Dockerfile      Render 用（既定 SQLite ビルド。TiDB/MySQL への切替はコメント参照）
python/           FastAPI 実装
  lambda_server.py  app（passkey/tenant/kintai ルータを /api 配下、/html 配信、/ は redirect）
  database.py       既定 SQLite / env で MySQL(TiDB) / DATABASE_URL 優先
  requirements.txt
```

## 共通の約束

- **ポートは環境変数 `POST`**（PORT ではない。既定 8080）。Render は `PORT` を注入するので、
  Python は `uvicorn ... --port $PORT`、C++ は Docker CMD で `POST=$PORT` に橋渡しする。
- **DB は既定 SQLite**（`test_db.db`、テーブルは起動時に自動作成）。
  永続化は **TiDB Cloud**：env `RDS_PROXY_HOST / USER_NAME / PASSWORD / DB_NAME`（Python は `DATABASE_URL` も可）。
- CORS 全許可。`/html/index.html` は 200（Render のヘルスチェックに使用）。`/` は `/html/index.html` へ redirect。

## ローカルで動かす

**Python（FastAPI）**
```sh
cd python
python -m venv .venv && . .venv/Scripts/activate      # mac/Linux は .venv/bin/activate
pip install -r requirements.txt
POST=8080 python lambda_server.py                      # http://localhost:8080/html/index.html
```

**C++（cpp-httplib）— Windows ローカル**

既定は **SQLite（依存ゼロ）**。`cpp/sqlpp11_cust.sln` を VS2022 で Release/x64 ビルド →
`cpp/` ディレクトリ（`html/` がある場所）から起動する：
```sh
cd cpp
POST=8080 ./x64/Release/sqlpp11_cust.exe        # http://localhost:8080/html/index.html
# DB はカレントに test_db.db を自動作成。DLL 不要。
```

**C++ を TiDB/MySQL で動かす（Windows・任意）**
1. `USE_MYSQL` を定義してビルド（`database.h` 冒頭の `#define USE_MYSQL 1` を有効化、
   もしくは MSBuild を `CL=-DUSE_MYSQL`（`/D` は Git Bash がパス変換して壊すのでダッシュ）で実行）。
2. 実行時に `libmysql.dll` を dlopen する。**`sh cpp/setup_local_dll.sh` を一度実行**すると、
   ローカルの MySQL 8.4 インストール（既定 `C:\prog\mysql-8.4.4-winx64`、`MYSQL_DIR` で変更可）から
   `libmysql.dll`＋openssl DLL を `x64/Release`・`x64/Debug`（exe の隣）にコピーする。
   → 以後 **PATH 設定不要**で exe が DLL を隣から読む。これらの DLL は `.gitignore` 済み（ローカル専用・非コミット）。
3. 接続情報を env で渡して起動：
   ```sh
   export PATH="/c/prog/mysql-8.4.4-winx64/lib:/c/prog/mysql-8.4.4-winx64/bin:$PATH"
   RDS_PROXY_HOST=... TIDB_PORT=4000 USER_NAME=... PASSWORD=... DB_NAME=test \
   POST=8080 ./x64/Release/sqlpp11_cust.exe
   ```

**C++ — Linux/Docker**
```sh
cd cpp && docker build -t kintai-cpp . && docker run -e PORT=8080 -p 8080:8080 \
  -e RDS_PROXY_HOST=... -e TIDB_PORT=4000 -e USER_NAME=... -e PASSWORD=... -e DB_NAME=test kintai-cpp
```

## Render へのデプロイ

1. この repo を push（`render.yaml` はリポ直下）。
2. Render → **New + → Blueprint** → この repo を選択 → 2サービス作成。
   - python … ネイティブ Python（`pip install` → `uvicorn`）
   - cpp … `cpp/Dockerfile` をビルド
3. `https://<service>.onrender.com/html/index.html` で確認。

無料枠は 15 分無アクセスでスピンダウン・起動約1分・**ディスク揮発**（永続化は TiDB へ）。

## TiDB Cloud

- Serverless（MySQL 互換・**TLS 必須**・port 4000）。接続情報は **env で注入し、リポジトリに秘密を置かない**（`secrets/` は gitignore 済み）。
- Python: `PyMySQL`（TLS 引数が要る）。C++: `USE_MYSQL` ビルド＋`libmysqlclient`（`cpp/Dockerfile` 末尾コメント）。

## ライセンス / 出典

元リポジトリ（passkey_server_cpp）に準じる。cpp-httplib(MIT), nlohmann/json(MIT), SQLite(public domain),
FastAPI/Uvicorn/SQLAlchemy(MIT 等), mbedtls 由来コード(Apache-2.0)。
