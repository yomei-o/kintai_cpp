#!/usr/bin/env python3
#
# 勤怠 API の検査。サーバ（Python 版でも C++ 版でも）に同じ手順を流し、応答を正規化して 1 行ずつ出す。
# 両方の出力を diff すれば機能パリティを確かめられる。
#
#   python tools/kintai_check.py http://localhost:8091 path/to/test_db.db > out_py.txt
#   python tools/kintai_check.py http://localhost:8092 path/to/test_db.db > out_cpp.txt
#   diff out_py.txt out_cpp.txt
#
# TiDB(MySQL) のサーバは DB に "mysql" を渡す（接続は env RDS_PROXY_HOST / TIDB_PORT / USER_NAME /
# PASSWORD / DB_NAME、要 pymysql）。店・ユーザ名を実行ごとに変え、最後に検査の行を全部消す。
#
# パスキーでのログインは自動化できないので、アカウント・セッション・テナント・メンバー、
# 月の集計を確かめるための打刻（前の月の決まった時刻）を DB に直接入れる。
# SQLite のときはサーバを起動してテーブルができた後で、空の DB に対して実行する。
#
import datetime
import json
import secrets
import sqlite3
import sys
import urllib.request

BASE = sys.argv[1].rstrip("/")
DB = sys.argv[2]
MYSQL = DB == "mysql"

RUN = secrets.token_hex(4) if MYSQL else "t"
GUID = ("c0ffee" + RUN + "0" * 18) if MYSQL else "0123456789abcdef0123456789abcdef"
ROLES = ("owner", "staff", "alice", "stranger")
SKEY = {u: secrets.token_hex(16) for u in ROLES} if MYSQL else {u: c * 32 for u, c in zip(ROLES, "abcd")}
USERS = {u: ("kc_" + RUN + "_" + u) if MYSQL else u for u in ROLES}

JST = datetime.timedelta(hours=9)
NOW = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def fmt(t):
    return t.strftime("%Y-%m-%d %H:%M:%S")


# 前の月（日本時間）
_j = NOW + JST
PY, PM = (_j.year, _j.month - 1) if _j.month > 1 else (_j.year - 1, 12)
MONTH = "%04d-%02d" % (PY, PM)


# 前の月の d 日 h:m（日本時間）→ UTC の文字列。d = 0 は前の月の前の日、d = 99 は前の月の最終日
def jt(d, h, m=0):
    first = datetime.datetime(PY, PM, 1)
    if d == 99:
        nxt = datetime.datetime(PY + (PM == 12), PM % 12 + 1, 1)
        day = nxt - datetime.timedelta(days=1)
    else:
        day = first + datetime.timedelta(days=d - 1)
    return fmt(day.replace(hour=h, minute=m) - JST)


class MyCon:
    def __init__(self):
        import os
        import ssl
        import pymysql
        self.c = pymysql.connect(host=os.environ["RDS_PROXY_HOST"], port=int(os.environ.get("TIDB_PORT", "4000")),
                                 user=os.environ["USER_NAME"], password=os.environ["PASSWORD"],
                                 database=os.environ["DB_NAME"], ssl=ssl.create_default_context())

    def execute(self, sql, args=()):
        cur = self.c.cursor()
        cur.execute(sql.replace("?", "%s"), args)
        return cur

    def commit(self):
        self.c.commit()

    def close(self):
        self.c.close()


def connect():
    return MyCon() if MYSQL else sqlite3.connect(DB)


def tenant_id(con):
    row = con.execute("SELECT id FROM tenant WHERE guid = ?", (GUID,)).fetchone()
    return row[0] if row else None


def punch(con, tid, who, kind, at, deleted=0):
    con.execute("INSERT INTO kintai_punch (tenant_id, user, kind, punched_at, source, deleted, created_at) "
                "VALUES (?, ?, ?, ?, 'qr', ?, ?)", (tid, USERS[who], kind, at, deleted, at))
    return con.execute("SELECT MAX(id) FROM kintai_punch WHERE tenant_id = ?", (tid,)).fetchone()[0]


PID = {}   # 名前 → 入れた打刻の id


def seed():
    con = connect()
    t = fmt(NOW)
    exp = fmt(NOW + datetime.timedelta(hours=1))
    for u in ROLES:
        con.execute("INSERT INTO account (user, status, role, created_at) VALUES (?, 'approved', 'member', ?)",
                    (USERS[u], t))
        con.execute("INSERT INTO session (user, skey, expire, created_at) VALUES (?, ?, ?, ?)",
                    (USERS[u], SKEY[u], exp, t))
    con.execute("INSERT INTO tenant (guid, name, created_by, created_at) VALUES (?, 'テスト食堂', ?, ?)",
                (GUID, USERS["owner"], t))
    tid = tenant_id(con)
    for u, role in (("owner", "admin"), ("staff", "member"), ("alice", "member")):
        con.execute("INSERT INTO tenant_member (tenant_id, user, status, role, created_at) "
                    "VALUES (?, ?, 'approved', ?, ?)", (tid, USERS[u], role, t))
    # 前の月の前の日の 23:00 に出勤して、前の月の 1 日 2:00 に退勤（前の前の月の分。前の月には出ない）
    punch(con, tid, "staff", "in", jt(0, 23))
    punch(con, tid, "staff", "out", jt(1, 2))
    # 3 日: 9:00〜18:00（540 分）
    punch(con, tid, "staff", "in", jt(3, 9))
    punch(con, tid, "staff", "out", jt(3, 18))
    # 4 日 22:00〜5 日 6:00（0 時をまたぐ。4 日の分で 480 分）
    punch(con, tid, "staff", "in", jt(4, 22))
    punch(con, tid, "staff", "out", jt(5, 6))
    # 5 日 10:00 出勤、退勤を押し忘れ
    PID["day5_in"] = punch(con, tid, "staff", "in", jt(5, 10))
    # 6 日 10:00〜15:30（330 分）
    PID["day6_in"] = punch(con, tid, "staff", "in", jt(6, 10))
    punch(con, tid, "staff", "out", jt(6, 15, 30))
    # 7 日 12:00 退勤だけ（出勤の押し忘れ）
    PID["day7_out"] = punch(con, tid, "staff", "out", jt(7, 12))
    # 8 日: 消した打刻は数えない
    punch(con, tid, "staff", "in", jt(8, 9), deleted=1)
    punch(con, tid, "staff", "out", jt(8, 17), deleted=1)
    # 最終日 23:00 出勤、翌月 1 日 1:00 退勤（前の月の分で 120 分）
    punch(con, tid, "staff", "in", jt(99, 23))
    punch(con, tid, "staff", "out", fmt(datetime.datetime.strptime(jt(99, 23), "%Y-%m-%d %H:%M:%S")
                                        + datetime.timedelta(hours=2)))
    # alice: 10 日 9:00〜12:00（180 分）
    PID["alice_in"] = punch(con, tid, "alice", "in", jt(10, 9))
    punch(con, tid, "alice", "out", jt(10, 12))
    # 1 年より古い打刻と修正リクエスト（状況の API で消える。SQLite のときだけ確かめる）
    if not MYSQL:
        old = fmt(NOW - datetime.timedelta(days=400))
        punch(con, tid, "alice", "in", old)
        con.execute("INSERT INTO kintai_request (tenant_id, user, action, kind, new_at, reason, status, created_at) "
                    "VALUES (?, ?, 'add', 'in', ?, '古い', 'pending', ?)", (tid, USERS["alice"], old, old))
    con.commit()
    con.close()


def cleanup_rows():
    con = connect()
    tid = tenant_id(con)
    if tid is not None:
        for t in ("kintai_punch", "kintai_request", "tenant_member"):
            con.execute("DELETE FROM " + t + " WHERE tenant_id = ?", (tid,))
        con.execute("DELETE FROM tenant WHERE id = ?", (tid,))
    for u in USERS.values():
        con.execute("DELETE FROM session WHERE user = ?", (u,))
        con.execute("DELETE FROM account WHERE user = ?", (u,))
    con.commit()
    con.close()


def call(path, body, who=None):
    h = {"Content-Type": "application/json"}
    if who:
        h["Authorization"] = "Bearer " + SKEY[who]
    req = urllib.request.Request(BASE + "/api/kintai/v0/" + path, data=json.dumps(body).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# 実行ごとに変わるものを伏せる: いまの時刻（10 分以内）、店の guid、ユーザ名の接頭辞、
# 打刻とリクエストの id（出てきた順に P1, P2 … / R1, R2 …）
IDS = {"P": {}, "R": {}}
UNAME = {v: k for k, v in USERS.items()}


def label(kind, v):
    if not isinstance(v, int) or isinstance(v, bool):
        return v
    m = IDS[kind]
    if v not in m:
        m[v] = kind + str(len(m) + 1)
    return m[v]


def is_recent(s):
    try:
        t = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return abs((t - NOW).total_seconds()) < 600


def norm(o):
    if isinstance(o, dict):
        is_req = "action" in o
        out = {}
        for k, v in o.items():
            if k == "id":
                v = label("R" if is_req else "P", v)
            elif k == "punch" and isinstance(v, int):
                v = label("P", v)
            elif isinstance(v, str) and is_recent(v):
                v = "<now>"
            elif k == "tenant" and v == GUID:
                v = "<tenant>"
            elif isinstance(v, str) and v in UNAME:
                v = UNAME[v]
            else:
                v = norm(v)
            out[k] = v
        return out
    if isinstance(o, list):
        return [norm(x) for x in o]
    return o


def step(label_, path, body, who=None):
    r = call(path, body, who)
    print(label_ + " " + path + " " + json.dumps(norm(r), ensure_ascii=False, sort_keys=True))
    return r


def main():
    seed()
    T = GUID
    print("00 month " + MONTH.replace(MONTH, "<prev month>"))
    step("01", "status", {"tenant": T})
    step("02", "status", {"tenant": T}, "stranger")
    step("03", "status", {"tenant": "nope"}, "staff")
    step("04", "status", {"tenant": T}, "staff")
    step("05", "punch", {"tenant": T, "kind": "x"}, "staff")
    step("06", "punch", {"tenant": T, "kind": "in"}, "staff")
    step("07", "status", {"tenant": T}, "staff")
    step("08", "punch", {"tenant": T, "kind": "out"}, "staff")
    step("09", "status", {"tenant": T}, "staff")

    step("10", "month", {"tenant": T, "month": "2026-13"}, "staff")
    step("11", "month", {"tenant": T, "month": "abc"}, "staff")
    step("12", "month", {"tenant": T}, "staff")
    step("13", "month", {"tenant": T, "month": MONTH}, "staff")
    step("14", "month", {"tenant": T, "month": MONTH, "user": USERS["alice"]}, "staff")
    step("15", "month", {"tenant": T, "month": MONTH, "all": True}, "staff")
    step("16", "month", {"tenant": T, "month": MONTH, "all": True}, "owner")
    step("17", "month", {"tenant": T, "month": MONTH, "user": USERS["staff"]}, "owner")
    step("18", "month", {"tenant": T, "month": MONTH, "user": 5}, "staff")

    step("20", "request", {"tenant": T, "action": "move", "reason": "x"}, "staff")
    step("21", "request", {"tenant": T, "action": "add", "reason": ""}, "staff")
    step("22", "request", {"tenant": T, "action": "add", "reason": "あ" * 67}, "staff")
    step("23", "request", {"tenant": T, "action": "delete", "punch": PID["alice_in"], "reason": "x"}, "staff")
    step("24", "request", {"tenant": T, "action": "delete", "reason": "x"}, "staff")
    step("25", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "reason": "x"}, "staff")
    step("26", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "reason": "x",
                           "at": fmt(NOW + datetime.timedelta(hours=1))}, "staff")
    step("27", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "reason": "x",
                           "at": "2026-1-01 00:00:00"}, "staff")
    step("28", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "reason": "x",
                           "at": "2026-02-30 00:00:00"}, "staff")
    step("29", "request", {"tenant": T, "action": "add", "reason": "x", "at": jt(5, 19)}, "staff")
    r_add = step("30", "request", {"tenant": T, "action": "add", "kind": "out", "at": jt(5, 19),
                                   "reason": "退勤を押し忘れました"}, "staff")
    r_chg = step("31", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "at": jt(6, 9, 30),
                                   "reason": "9:30 に来ました\n押すのが遅れました"}, "staff")
    step("32", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "at": jt(6, 9),
                           "reason": "x"}, "staff")
    r_del = step("33", "request", {"tenant": T, "action": "delete", "punch": PID["day7_out"],
                                   "reason": "間違えて押した"}, "staff")
    r_can = step("34", "request", {"tenant": T, "action": "change", "punch": PID["day5_in"], "kind": "out",
                                   "at": jt(5, 10), "reason": "あとで取り下げる"}, "staff")
    step("35", "request", {"tenant": T, "action": "change", "punch": PID["day6_in"], "kind": "x",
                           "at": jt(6, 9), "reason": "x"}, "staff")

    step("40", "requests", {"tenant": T}, "staff")
    step("41", "requests", {"tenant": T}, "owner")
    step("42", "requests", {"tenant": T, "mine": True}, "owner")
    step("43", "request_cancel", {"tenant": T, "id": r_can["request"]["id"]}, "owner")
    step("44", "request_cancel", {"tenant": T, "id": r_can["request"]["id"]}, "staff")
    step("45", "request_cancel", {"tenant": T, "id": r_can["request"]["id"]}, "staff")
    step("46", "request_cancel", {"tenant": T, "id": 999999}, "staff")

    step("50", "request_decide", {"tenant": T, "id": r_add["request"]["id"], "approve": True}, "staff")
    step("51", "request_decide", {"tenant": T, "id": r_add["request"]["id"], "approve": "yes"}, "owner")
    step("52", "request_decide", {"tenant": T, "id": r_add["request"]["id"], "approve": True}, "owner")
    step("53", "request_decide", {"tenant": T, "id": r_chg["request"]["id"], "approve": True}, "owner")
    step("54", "request_decide", {"tenant": T, "id": r_del["request"]["id"], "approve": False}, "owner")
    step("55", "request_decide", {"tenant": T, "id": r_del["request"]["id"], "approve": True}, "owner")
    step("56", "request_decide", {"tenant": T, "id": r_can["request"]["id"], "approve": True}, "owner")
    step("57", "month", {"tenant": T, "month": MONTH}, "staff")
    step("58", "requests", {"tenant": T}, "staff")
    # 消された打刻を直そうとしても弾かれる（day6_in は 53 の承認で消えた）
    step("59", "request", {"tenant": T, "action": "delete", "punch": PID["day6_in"], "reason": "x"}, "staff")

    if not MYSQL:
        con = connect()
        lim = fmt(NOW - datetime.timedelta(days=365))
        op = con.execute("SELECT COUNT(*) FROM kintai_punch WHERE punched_at < ?", (lim,)).fetchone()[0]
        orq = con.execute("SELECT COUNT(*) FROM kintai_request WHERE created_at < ?", (lim,)).fetchone()[0]
        con.close()
        print("90 cleanup old_punches=%d old_requests=%d" % (op, orq))


if MYSQL:
    try:
        main()
    finally:
        cleanup_rows()
else:
    main()
