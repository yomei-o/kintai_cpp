CREATE TABLE kintai_punch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    kind TEXT NOT NULL,
    punched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    request_id INTEGER,
    deleted INTEGER NOT NULL,
    deleted_by_request INTEGER,
    created_at TEXT)
;

CREATE TABLE kintai_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    punch_id INTEGER,
    old_kind TEXT,
    old_at TEXT,
    kind TEXT,
    new_at TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT)
;
