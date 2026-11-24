from sqlalchemy import (
    Column,
    Integer,
    Text,
    DateTime,
)
from database import Base


# 打刻。書き換えない。修正リクエストを承認したら、元の行は deleted = 1（deleted_by_request に
# リクエストの id）にして、直した打刻を新しい行（source = "request"、request_id）として足す。
#   kind は "in"（出勤）/ "out"（退勤）。punched_at は naive な UTC
#   source は "qr"（店員が打刻）/ "request"（修正リクエストの承認で入った）
class KintaiPunch(Base):
    __tablename__ = "kintai_punch"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="ID")
    tenant_id = Column(Integer, nullable=False)
    user = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    punched_at = Column(DateTime, nullable=False)
    source = Column(Text, nullable=False)
    request_id = Column(Integer)
    deleted = Column(Integer, nullable=False)
    deleted_by_request = Column(Integer)
    created_at = Column(DateTime)


# 打刻の修正リクエスト。店員が出し、お店の管理者が承認・却下する
#   action は "change"（時刻・種類を直す）/ "add"（足す）/ "delete"（消す）
#   change / delete は punch_id の打刻が対象で、出した時点の中身を old_kind / old_at に写しておく
#   change / add は直したあとの kind / new_at を持つ
#   status は "pending" / "approved" / "rejected" / "canceled"
class KintaiRequest(Base):
    __tablename__ = "kintai_request"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="ID")
    tenant_id = Column(Integer, nullable=False)
    user = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    punch_id = Column(Integer)
    old_kind = Column(Text)
    old_at = Column(DateTime)
    kind = Column(Text)
    new_at = Column(DateTime)
    reason = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    decided_by = Column(Text)
    decided_at = Column(DateTime)
    created_at = Column(DateTime)
