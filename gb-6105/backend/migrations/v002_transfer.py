# 注册全部表后再 create_all：v002 只补缺的卷宗与调拨表，不影响既有表。
from src.models import audit, cases, reviews, runs, transfers, users  # noqa: F401
from src.models.base import metadata


def upgrade(connection):
    # 卷宗、在册证书、调拨单与调拨单行均在 transfers 中注册。
    metadata.create_all(connection)
