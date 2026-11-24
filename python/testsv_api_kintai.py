#
# 勤怠（QR で打刻）
#
# 店 = テナント（guid で指定）。打刻するのはそのテナントの承認済みメンバー（パスキーでログイン）。
# 扉に貼る固定の QR は /html/clock.html?tenant=<guid>。
#
#   /api/kintai/v0/status          {tenant} 自分の最後の打刻と、次に押すほう（in / out）
#   /api/kintai/v0/punch           {tenant, kind} 打刻する（kind は in = 出勤 / out = 退勤）
#   /api/kintai/v0/month           {tenant, month[, user | all]} 月（"YYYY-MM"、日本時間）の勤務の集計
#                                  （自分の分。user で他の人・all で全員を見られるのはお店の管理者だけ）
#   /api/kintai/v0/request         {tenant, action, punch, kind, at, reason} 打刻の修正をお願いする
#                                  （action は change = 直す / add = 足す / delete = 消す）
#   /api/kintai/v0/requests        {tenant[, mine]} 修正リクエストの一覧（管理者は全員分、ほかは自分の分）
#   /api/kintai/v0/request_cancel  {tenant, id} 自分の承認待ちのリクエストを取り下げる
#   /api/kintai/v0/request_decide  {tenant, id, approve} 承認・却下する（お店の管理者だけ）
#
# 時刻は API ではすべて naive な UTC の "YYYY-MM-DD HH:MM:SS"（日本時間への換算はページ側）。
# 月の集計: その人の打刻を時刻順に並べ、出勤のあとの最初の退勤（24 時間以内）と組にする。
# 0 時をまたいだ勤務は出勤した日（日本時間）の分。組にならない打刻は打刻漏れとして返す。
# 打刻と修正リクエストは 1 年（365 日）で消す（打刻・状況の API の中で 1 時間に 1 回）。
# C++ 版は cpp/main/testsv_api_kintai.h。検査の順番・失敗時の message・集計の手順を揃えてある。
#
import datetime
import re
import threading
import time

from fastapi import APIRouter, Request, Header, Depends
from sqlalchemy.orm import Session

from database import engine
import model_kintai
import model_tenant
from testsv_api_passkey import (
    ACCOUNT_APPROVED,
    ROLE_ADMIN,
    PasskeyError,
    arun,
    extract_bearer_token,
    get_db,
    read_body,
    session_user,
    utcnow,
)
from testsv_api_tenant import (
    get_tenant,
    tenant_member_of,
)

router = APIRouter()

Punch = model_kintai.KintaiPunch
Req = model_kintai.KintaiRequest

KINDS = ("in", "out")
ACTIONS = ("change", "add", "delete")
KEEP_DAYS = 365
SHIFT_MAX = datetime.timedelta(hours=24)
JST = datetime.timedelta(hours=9)
FUTURE_SLACK = datetime.timedelta(minutes=5)
REASON_MAX_BYTES = 200
CLEAN_INTERVAL_SEC = 3600
REQUESTS_LIMIT = 200


def init_kintai():
    print("init_kintai()")
    model_kintai.Base.metadata.create_all(bind=engine)

init_kintai()


# 秒より下は持たない（C++ 版と同じ DB を読み書きするので）
def now() -> datetime.datetime:
    return utcnow().replace(microsecond=0)


def fmt(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") if t is not None else None


# "YYYY-MM-DD HH:MM:SS" をきっちり読む（書き戻して同じ文字列になるものだけ）
def parse_time(s):
    if not isinstance(s, str) or len(s) != 19:
        return None
    try:
        t = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return t if fmt(t) == s else None


# "YYYY-MM"（日本時間の月）→ その月の始まりと次の月の始まり（どちらも UTC）
def parse_month(s):
    if not isinstance(s, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", s):
        return None
    y, m = int(s[:4]), int(s[5:])
    if y < 2000 or y > 2099 or m < 1 or m > 12:
        return None
    start = datetime.datetime(y, m, 1) - JST
    end = datetime.datetime(y + (m == 12), m % 12 + 1, 1) - JST
    return start, end


def jst_date(t) -> str:
    return (t + JST).strftime("%Y-%m-%d")


def int_of(body: dict, key: str):
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


# 理由は 1〜200 バイト、改行以外の制御文字なし
def valid_reason(s) -> bool:
    if not isinstance(s, str):
        return False
    b = s.encode("utf-8")
    if len(b) == 0 or len(b) > REASON_MAX_BYTES:
        return False
    return all(c >= 0x20 and c != 0x7F or c == 0x0A for c in b)


def punch_to_dict(p) -> dict:
    return {"id": p.id, "kind": p.kind, "at": fmt(p.punched_at), "source": p.source}


def request_to_dict(r) -> dict:
    return {
        "id": r.id,
        "user": r.user,
        "action": r.action,
        "punch": r.punch_id,
        "old_kind": r.old_kind,
        "old_at": fmt(r.old_at),
        "kind": r.kind,
        "at": fmt(r.new_at),
        "reason": r.reason,
        "status": r.status,
        "decided_by": r.decided_by,
        "decided_at": fmt(r.decided_at),
        "created_at": fmt(r.created_at),
    }


# 1 年たった打刻と修正リクエストを消す。1 時間に 1 回だけ
_clean_lock = threading.Lock()
_clean_last = 0.0


def cleanup(db: Session):
    global _clean_last
    with _clean_lock:
        if time.time() - _clean_last < CLEAN_INTERVAL_SEC:
            return
        _clean_last = time.time()
    limit = now() - datetime.timedelta(days=KEEP_DAYS)
    db.query(Punch).filter(Punch.punched_at < limit).delete(synchronize_session=False)
    db.query(Req).filter(Req.created_at < limit).delete(synchronize_session=False)
    db.commit()


# 店員の入口: ログインしていて、そのテナントの承認済みメンバー
def staff_of(db: Session, skey: str, body: dict):
    user = session_user(db, skey)
    t = get_tenant(db, body)
    m = tenant_member_of(db, t.id, user)
    return user, t, m


def require_admin(m):
    if m.role != ROLE_ADMIN:
        raise PasskeyError("tenant admin only")


def last_punch(db: Session, tenant_id: int, user: str):
    return db.query(Punch).filter(Punch.tenant_id == tenant_id, Punch.user == user, Punch.deleted == 0) \
        .order_by(Punch.punched_at.desc(), Punch.id.desc()).first()


# 次に押すほう: 最後の打刻が出勤で、24 時間たっていなければ退勤
def next_kind(p) -> str:
    if p is not None and p.kind == "in" and now() - p.punched_at < SHIFT_MAX:
        return "out"
    return "in"


def get_my_active_punch(db: Session, tenant_id: int, user: str, body: dict):
    pid = int_of(body, "punch")
    if pid is None:
        raise PasskeyError("invalid request")
    p = db.query(Punch).filter(Punch.id == pid, Punch.tenant_id == tenant_id, Punch.user == user,
                               Punch.deleted == 0).first()
    if p is None:
        raise PasskeyError("unknown punch")
    return p


# 直したあとの時刻: 1 年以内で、先の時刻は 5 分まで
def get_new_time(body: dict):
    t = parse_time(body.get("at"))
    if t is None:
        raise PasskeyError("invalid time")
    n = now()
    if t > n + FUTURE_SLACK or t < n - datetime.timedelta(days=KEEP_DAYS):
        raise PasskeyError("invalid time")
    return t


def get_request_row(db: Session, tenant_id: int, body: dict):
    rid = int_of(body, "id")
    if rid is None:
        raise PasskeyError("invalid request")
    r = db.query(Req).filter(Req.id == rid, Req.tenant_id == tenant_id).first()
    if r is None:
        raise PasskeyError("unknown request")
    return r


#
# 月の集計
#

# 1 人分。punches は [start - 24h, end + 24h) の有効な打刻を (時刻, id) の順に
def summarize(user: str, punches: list, start, end) -> dict:
    n = now()
    raw = []

    def emit(state, pin, pout):
        anchor = pin.punched_at if pin is not None else pout.punched_at
        raw.append((anchor, state, pin, pout))

    pending = None
    for p in punches:
        if p.kind == "in":
            if pending is not None:
                emit("missing_out", pending, None)
            pending = p
        else:
            if pending is not None and p.punched_at - pending.punched_at <= SHIFT_MAX:
                emit("ok", pending, p)
                pending = None
            else:
                if pending is not None:
                    emit("missing_out", pending, None)
                    pending = None
                emit("missing_in", None, p)
    if pending is not None:
        emit("working" if n - pending.punched_at < SHIFT_MAX else "missing_out", pending, None)

    shifts = []
    days = set()
    total = 0
    for anchor, state, pin, pout in raw:
        if anchor < start or anchor >= end:
            continue
        minutes = None
        if state == "ok":
            minutes = int((pout.punched_at - pin.punched_at).total_seconds()) // 60
            total += minutes
            days.add(jst_date(anchor))
        shifts.append({
            "date": jst_date(anchor),
            "state": state,
            "in": punch_to_dict(pin) if pin is not None else None,
            "out": punch_to_dict(pout) if pout is not None else None,
            "minutes": minutes,
        })
    return {"user": user, "days": len(days), "minutes": total, "shifts": shifts}


#
# API
#

def kintai_status(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    cleanup(db)
    p = last_punch(db, t.id, user)
    pending = db.query(Req).filter(Req.tenant_id == t.id, Req.user == user, Req.status == "pending").count()
    return {
        "status": "success",
        "tenant": t.guid,
        "name": t.name,
        "user": user,
        "my_role": m.role,
        "last": punch_to_dict(p) if p is not None else None,
        "next": next_kind(p),
        "pending": pending,
    }


def kintai_punch(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    kind = body.get("kind")
    if kind not in KINDS:
        raise PasskeyError("invalid kind")
    cleanup(db)
    n = now()
    p = Punch(tenant_id=t.id, user=user, kind=kind, punched_at=n, source="qr", deleted=0, created_at=n)
    db.add(p)
    db.commit()
    return {"status": "success", "punch": punch_to_dict(p), "next": next_kind(p)}


def kintai_month(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    rng = parse_month(body.get("month"))
    if rng is None:
        raise PasskeyError("invalid month")
    start, end = rng
    if body.get("all") is True:
        require_admin(m)
        users = None
    else:
        target = body.get("user")
        if target is None or target == "":
            target = user
        if not isinstance(target, str):
            raise PasskeyError("invalid request")
        if target != user:
            require_admin(m)
        users = [target]
    rows = db.query(Punch).filter(Punch.tenant_id == t.id, Punch.deleted == 0,
                                  Punch.punched_at >= start - SHIFT_MAX, Punch.punched_at < end + SHIFT_MAX)
    if users is not None:
        rows = rows.filter(Punch.user == users[0])
    rows = rows.order_by(Punch.punched_at, Punch.id).all()
    if users is None:
        members = {x.user for x in db.query(model_tenant.TenantMember).filter(
            model_tenant.TenantMember.tenant_id == t.id, model_tenant.TenantMember.status == ACCOUNT_APPROVED)}
        users = sorted(members | {p.user for p in rows})
    return {
        "status": "success",
        "tenant": t.guid,
        "month": body["month"],
        "users": [summarize(u, [p for p in rows if p.user == u], start, end) for u in users],
    }


def kintai_request(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    action = body.get("action")
    if action not in ACTIONS:
        raise PasskeyError("invalid action")
    reason = body.get("reason")
    if not valid_reason(reason):
        raise PasskeyError("invalid reason")
    r = Req(tenant_id=t.id, user=user, action=action, reason=reason, status="pending", created_at=now())
    if action in ("change", "delete"):
        p = get_my_active_punch(db, t.id, user, body)
        if db.query(Req).filter(Req.tenant_id == t.id, Req.punch_id == p.id, Req.status == "pending").first():
            raise PasskeyError("already requested")
        r.punch_id = p.id
        r.old_kind = p.kind
        r.old_at = p.punched_at
    if action in ("change", "add"):
        kind = body.get("kind")
        if action == "change" and kind is None:
            kind = r.old_kind
        if kind not in KINDS:
            raise PasskeyError("invalid kind")
        r.kind = kind
        r.new_at = get_new_time(body)
    db.add(r)
    db.commit()
    return {"status": "success", "request": request_to_dict(r)}


def kintai_requests(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    q = db.query(Req).filter(Req.tenant_id == t.id)
    if m.role != ROLE_ADMIN or body.get("mine") is True:
        q = q.filter(Req.user == user)
    rows = q.order_by(Req.id.desc()).limit(REQUESTS_LIMIT).all()
    return {
        "status": "success",
        "requests": [request_to_dict(r) for r in rows],
        "pending": sum(1 for r in rows if r.status == "pending"),
    }


def kintai_request_cancel(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    r = get_request_row(db, t.id, body)
    if r.user != user:
        raise PasskeyError("unknown request")
    if r.status != "pending":
        raise PasskeyError("already decided")
    r.status = "canceled"
    r.decided_by = user
    r.decided_at = now()
    db.commit()
    return {"status": "success", "request": request_to_dict(r)}


def kintai_request_decide(db: Session, skey: str, body: dict) -> dict:
    user, t, m = staff_of(db, skey, body)
    require_admin(m)
    r = get_request_row(db, t.id, body)
    if r.status != "pending":
        raise PasskeyError("already decided")
    approve = body.get("approve")
    if not isinstance(approve, bool):
        raise PasskeyError("invalid request")
    n = now()
    if approve:
        if r.action in ("change", "delete"):
            p = db.query(Punch).filter(Punch.id == r.punch_id, Punch.deleted == 0).first()
            if p is None:
                raise PasskeyError("punch not found")
            p.deleted = 1
            p.deleted_by_request = r.id
        if r.action in ("change", "add"):
            db.add(Punch(tenant_id=t.id, user=r.user, kind=r.kind, punched_at=r.new_at, source="request",
                         request_id=r.id, deleted=0, created_at=n))
    r.status = "approved" if approve else "rejected"
    r.decided_by = user
    r.decided_at = n
    db.commit()
    return {"status": "success", "request": request_to_dict(r)}


#
# routes
#

def route(path, func_):
    async def handler(request: Request,
                      authorization: str = Header(None),
                      db: Session = Depends(get_db)):
        return await arun(db, func_, extract_bearer_token(authorization), await read_body(request))
    handler.__name__ = "api_" + func_.__name__
    router.post(path)(handler)


route("/kintai/v0/status", kintai_status)
route("/kintai/v0/punch", kintai_punch)
route("/kintai/v0/month", kintai_month)
route("/kintai/v0/request", kintai_request)
route("/kintai/v0/requests", kintai_requests)
route("/kintai/v0/request_cancel", kintai_request_cancel)
route("/kintai/v0/request_decide", kintai_request_decide)
