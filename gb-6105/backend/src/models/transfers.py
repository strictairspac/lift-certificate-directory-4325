from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, Integer, JSON, String, Table
from .base import metadata


# 资质卷宗：登记上限 capacity，满卷仍允许等量换入换出。
dossiers = Table(
    "dossiers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(48), unique=True, nullable=False),
    Column("title", String(120), nullable=False),
    Column("capacity", Integer, nullable=False),
    Column("created_by", String(48), nullable=False),
    CheckConstraint("capacity >= 1", name="ck_dossier_capacity_positive"),
)


# 卷宗在册证书。owner_id 即当前归属卷宗；调拨只改归属，不新增/删除证书。
dossier_certificates = Table(
    "dossier_certificates", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(40), unique=True, nullable=False),
    Column("lift", String(40), nullable=False),
    Column("expires_on", String(10), nullable=False),
    Column("revoked", String(1), nullable=False, server_default="0"),
    Column("owner_id", ForeignKey("dossiers.id"), nullable=False, index=True),
    CheckConstraint("revoked in ('0','1')", name="ck_dossier_cert_revoked"),
)


# 调拨单：request_key 为幂等键，同单重试命中唯一约束，绝不重复调拨。
transfer_orders = Table(
    "transfer_orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_key", String(64), unique=True, nullable=False),
    Column("left_dossier_id", ForeignKey("dossiers.id"), nullable=False),
    Column("right_dossier_id", ForeignKey("dossiers.id"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("payload", JSON, nullable=False),
)


# 调拨单行：逐条记录每张证书的原归属与新归属，供检证页面复核。
transfer_order_items = Table(
    "transfer_order_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", ForeignKey("transfer_orders.id"), nullable=False, index=True),
    Column("certificate_code", String(40), nullable=False),
    Column("source_dossier_id", ForeignKey("dossiers.id"), nullable=False),
    Column("target_dossier_id", ForeignKey("dossiers.id"), nullable=False),
)
