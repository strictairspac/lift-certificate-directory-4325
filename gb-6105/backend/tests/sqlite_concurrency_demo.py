"""并发同单调拨模拟（仅标准库 sqlite3）。

镜像 services/transfers.py 的顺序：
  幂等预检 -> BEGIN IMMEDIATE（取写锁，等价 FOR UPDATE 串行化）
  -> 锁后再查 request_key -> 校验 -> 写单/改归属 -> 提交。
两个连接使用同一 request_key，断言只有一张调拨单、证书只移动一次、
后到者拿到原单号（幂等），即使证书后来被调回也不重复调拨。
"""

import json
import sqlite3
import sys

sys.path.insert(0, "tests")
from sqlite_transfer_integration import (  # noqa: E402
    SCHEMA, request_key_of, owners, point_check,
)


def open_db(shared_uri=None):
    if shared_uri:
        conn = sqlite3.connect(shared_uri, uri=True)
    else:
        conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn):
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO dossiers(code,title,capacity,created_by) VALUES('DOS-A','东',6,'s')")
    conn.execute("INSERT INTO dossiers(code,title,capacity,created_by) VALUES('DOS-B','西',6,'s')")
    rows = [(f"YT-Q0{i}", f"YT-L0{i}", "2026-12-31", 0, 1 if i <= 5 else 2)
            for i in range(1, 10)]
    conn.executemany("INSERT INTO dossier_certificates(code,lift,expires_on,revoked,owner_id)"
                     " VALUES(?,?,?,?,?)", rows)
    conn.commit()


def service_sequence(conn, payload):
    """完整复刻服务端顺序，返回 (order_id, created)。"""
    key = request_key_of(payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 锁后幂等复查
        row = conn.execute(
            "SELECT id FROM transfer_orders WHERE request_key=?", (key,)).fetchone()
        if row is not None:
            conn.commit()
            return row["id"], False
        lo, ro = payload["left_codes"], payload["right_codes"]
        left, right = payload["left_dossier_id"], payload["right_dossier_id"]
        have = {r["code"]: r["owner_id"] for r in conn.execute(
            "SELECT code, owner_id FROM dossier_certificates").fetchall()}
        for c in lo:
            assert have.get(c) == left, f"{c} 已调走，整单回滚"
        for c in ro:
            assert have.get(c) == right, f"{c} 已调走，整单回滚"
        cur = conn.execute(
            "INSERT INTO transfer_orders(request_key,left_dossier_id,right_dossier_id,"
            "status,quantity,actor,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",
            (key, left, right, "confirmed", len(lo), "op", 1, json.dumps(payload)))
        oid = cur.lastrowid
        conn.executemany(
            "INSERT INTO transfer_order_items(order_id,certificate_code,"
            "source_dossier_id,target_dossier_id) VALUES(?,?,?,?)",
            [(oid, c, left, right) for c in lo] + [(oid, c, right, left) for c in ro])
        for c in lo:
            conn.execute("UPDATE dossier_certificates SET owner_id=? WHERE code=? AND owner_id<>?",
                         (right, c, right))
        for c in ro:
            conn.execute("UPDATE dossier_certificates SET owner_id=? WHERE code=? AND owner_id<>?",
                         (left, c, left))
        conn.commit()
        return oid, True
    except Exception:
        conn.rollback()
        raise


def main():
    # 共享内存库，需要 file: URI + cache=shared 才能多连接访问。
    uri = "file:concurrency_demo?mode=memory&cache=shared"
    bootstrap = open_db(uri)
    seed(bootstrap)

    payload = {"left_dossier_id": 1, "right_dossier_id": 2,
               "left_codes": ["YT-Q01", "YT-Q02"], "right_codes": ["YT-Q06", "YT-Q07"]}

    c1, c2 = open_db(uri), open_db(uri)
    oid1, created1 = service_sequence(c1, payload)
    print("连接一首次：", oid1, created1)
    assert created1 is True

    oid2, created2 = service_sequence(c2, payload)
    print("连接二同单：", oid2, created2)
    assert created2 is False and oid2 == oid1, "并发/重试同单必须幂等返回原单"

    order_count = bootstrap.execute("SELECT COUNT(*) c FROM transfer_orders").fetchone()["c"]
    assert order_count == 1, f"只能有一张调拨单，实际 {order_count}"
    item_count = bootstrap.execute("SELECT COUNT(*) c FROM transfer_order_items").fetchone()["c"]
    assert item_count == 4, f"应有 4 条明细，实际 {item_count}"

    final = owners(bootstrap)
    assert final["YT-Q01"] == 2 and final["YT-Q02"] == 2
    assert final["YT-Q06"] == 1 and final["YT-Q07"] == 1
    print("归属（仅移动一次）：", {k: v for k, v in final.items() if k in
          ("YT-Q01", "YT-Q02", "YT-Q06", "YT-Q07")})

    # 证书后来调回原卷，同单再试仍幂等、不再移动
    bootstrap.execute("UPDATE dossier_certificates SET owner_id=1 WHERE code='YT-Q01'")
    bootstrap.commit()
    oid3, created3 = service_sequence(c1, payload)
    assert created3 is False and oid3 == oid1
    assert owners(bootstrap)["YT-Q01"] == 1, "调回的证书不应被同单再次移动"

    # 点查：确认后 YT-Q02 仍在新卷，YT-Q01 因被手动调回而在旧卷（归属随状态变化）
    pc = point_check(bootstrap, ["YT-Q01", "YT-Q02"])
    print("点查：", pc)
    assert pc["YT-Q02"][0] == "DOS-B"

    print("\n并发幂等模拟全部通过：同单一单、只调拨一次、调回后仍不重复。")


if __name__ == "__main__":
    main()
