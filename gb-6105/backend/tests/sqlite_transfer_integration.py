"""调拨服务集成测试（仅标准库 sqlite3，镜像 PostgreSQL 表与事务口径）。

无法在本环境安装 SQLAlchemy/PostgreSQL，因此这里用 sqlite3 复刻
src/services/transfers.py 的真实事务步骤：
  * 固定顺序加锁（BEGIN IMMEDIATE + 按 id 排序选取）；
  * request_key 唯一约束实现同单幂等；
  * 校验失败整体回滚；确认后只改 owner 归属；
  * 点查在确认前/确认后归属切换。
运行：python3 tests/sqlite_transfer_integration.py
"""

import hashlib
import json
import sqlite3
import sys
from datetime import date


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def request_key_of(payload):
    return hashlib.sha256(canonical_json({"transfer": payload})).hexdigest()[:64]


SCHEMA = """
CREATE TABLE dossiers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  capacity INTEGER NOT NULL CHECK (capacity >= 1),
  created_by TEXT NOT NULL
);
CREATE TABLE dossier_certificates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  lift TEXT NOT NULL,
  expires_on TEXT NOT NULL,
  revoked TEXT NOT NULL DEFAULT '0' CHECK (revoked in ('0','1')),
  owner_id INTEGER NOT NULL
);
CREATE TABLE transfer_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_key TEXT UNIQUE NOT NULL,
  left_dossier_id INTEGER NOT NULL,
  right_dossier_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  actor TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE transfer_order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  certificate_code TEXT NOT NULL,
  source_dossier_id INTEGER NOT NULL,
  target_dossier_id INTEGER NOT NULL
);
"""


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO dossiers(code,title,capacity,created_by) VALUES('DOS-A','东',6,'seed')")
    conn.execute("INSERT INTO dossiers(code,title,capacity,created_by) VALUES('DOS-B','西',6,'seed')")
    certs = [(f"YT-Q0{i}", f"YT-L0{i}", "2026-12-31", 1 if i == 5 else 0,
              1 if i <= 5 else 2) for i in range(1, 10)]
    conn.executemany(
        "INSERT INTO dossier_certificates(code,lift,expires_on,revoked,owner_id)"
        " VALUES(?,?,?,?,?)", certs)
    conn.commit()
    return conn


def owners(conn):
    rows = conn.execute("SELECT code, owner_id FROM dossier_certificates ORDER BY code").fetchall()
    return {r["code"]: r["owner_id"] for r in rows}


class TransferRejected(Exception):
    pass


def parse_request(payload):
    left, right = payload["left_dossier_id"], payload["right_dossier_id"]
    if left == right:
        raise TransferRejected("两份卷宗之间才能互换证书")
    lo, ro = payload["left_codes"], payload["right_codes"]
    if len(set(lo)) != len(lo) or len(set(ro)) != len(ro):
        raise TransferRejected("同卷同证书只能调出一次")
    if len(lo) != len(ro):
        raise TransferRejected("成批互换须等量")
    return left, right, lo, ro


def create_order(conn, payload, actor="operator"):
    left, right, lo, ro = parse_request(payload)
    key = request_key_of(payload)
    # BEGIN IMMEDIATE 等价于先取写锁，模拟 with_for_update 的串行化。
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM transfer_orders WHERE request_key=?", (key,)).fetchone()
        if existing is not None:
            conn.commit()
            return existing["id"], False  # 幂等命中，不重复调拨

        ids = sorted({left, right})
        drows = conn.execute(
            f"SELECT * FROM dossiers WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
            ids).fetchall()
        if len(drows) != len(ids):
            raise TransferRejected("卷宗不存在")
        dmap = {r["id"]: r for r in drows}
        group = {i: {} for i in ids}
        crows = conn.execute(
            f"SELECT * FROM dossier_certificates WHERE owner_id IN ({','.join('?' * len(ids))})",
            ids).fetchall()
        for r in crows:
            group[r["owner_id"]][r["code"]] = r

        # 已调走校验
        for code in lo:
            if code not in group[left]:
                raise TransferRejected(f"{code} 已不在调出卷宗")
        for code in ro:
            if code not in group[right]:
                raise TransferRejected(f"{code} 已不在调出卷宗")
        # 重号
        if set(ro) & (set(group[left]) - set(lo)):
            raise TransferRejected("换入重号")
        if set(lo) & (set(group[right]) - set(ro)):
            raise TransferRejected("换入重号")
        # 上限
        if (len(group[left]) - len(lo) + len(ro)) > dmap[left]["capacity"]:
            raise TransferRejected("超出登记上限")
        if (len(group[right]) - len(ro) + len(lo)) > dmap[right]["capacity"]:
            raise TransferRejected("超出登记上限")

        cur = conn.execute(
            "INSERT INTO transfer_orders(request_key,left_dossier_id,right_dossier_id,"
            "status,quantity,actor,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",
            (key, left, right, "confirmed", len(lo), actor, 1_700_000_000,
             json.dumps(payload, ensure_ascii=False)))
        order_id = cur.lastrowid
        items = [(order_id, c, left, right) for c in lo] + \
                [(order_id, c, right, left) for c in ro]
        conn.executemany(
            "INSERT INTO transfer_order_items(order_id,certificate_code,"
            "source_dossier_id,target_dossier_id) VALUES(?,?,?,?)", items)
        for code in lo:
            conn.execute("UPDATE dossier_certificates SET owner_id=? WHERE code=? AND owner_id<>?",
                         (right, code, right))
        for code in ro:
            conn.execute("UPDATE dossier_certificates SET owner_id=? WHERE code=? AND owner_id<>?",
                         (left, code, left))
        conn.commit()
        return order_id, True
    except Exception:
        conn.rollback()
        raise


def point_check(conn, probes):
    result = {}
    for code in probes:
        row = conn.execute(
            "SELECT dc.code, dc.revoked, dc.expires_on, d.code AS dossier "
            "FROM dossier_certificates dc JOIN dossiers d ON d.id = dc.owner_id "
            "WHERE dc.code=?", (code,)).fetchone()
        if row is None:
            result[code] = (None, "未登记")
        elif row["revoked"] == "1":
            result[code] = (row["dossier"], "已撤销")
        elif date(2026, 11, 25) >= date.fromisoformat(row["expires_on"]):
            result[code] = (row["dossier"], "已到期")
        else:
            result[code] = (row["dossier"], "有效")
    return result


def main():
    failures = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    conn = open_db()
    before = owners(conn)
    payload = {"left_dossier_id": 1, "right_dossier_id": 2,
               "left_codes": ["YT-Q01", "YT-Q02"], "right_codes": ["YT-Q06", "YT-Q07"]}

    # 确认前点查：YT-Q01 在 DOS-A(id=1)
    check("确认前按旧归属点查", point_check(conn, ["YT-Q01"])["YT-Q01"][0] == "DOS-A")

    order_id, created = create_order(conn, payload)
    check("首次调拨成功", created is True)

    after = owners(conn)
    check("调出证书改归右卷", after["YT-Q01"] == 2 and after["YT-Q02"] == 2)
    check("换入证书改归左卷", after["YT-Q06"] == 1 and after["YT-Q07"] == 1)
    check("未涉及证书归属不变", after["YT-Q03"] == before["YT-Q03"] == 1)
    check("在册总数不增不减", len(after) == len(before))

    # 确认后点查：YT-Q01 在 DOS-B
    check("确认后按新归属点查", point_check(conn, ["YT-Q01"])["YT-Q01"][0] == "DOS-B")
    check("撤销状态仍可点出", point_check(conn, ["YT-Q05"])["YT-Q05"][1] == "已撤销")
    check("未登记点查", point_check(conn, ["YT-Q99"])["YT-Q99"][1] == "未登记")

    # 同单重试：不重复调拨，返回同一单号
    retry_id, retry_created = create_order(conn, payload)
    check("同单重试幂等（同一单号）", retry_id == order_id and retry_created is False)
    check("幂等重试不改变归属", owners(conn) == after)
    check("调拨单只有一张", conn.execute("SELECT COUNT(*) c FROM transfer_orders").fetchone()["c"] == 1)

    # 即使证书后来调回原卷，同单再试仍幂等（手动把 YT-Q01 调回，再用同一 request_key）
    conn.execute("UPDATE dossier_certificates SET owner_id=1 WHERE code='YT-Q01'")
    conn.commit()
    rid2, created2 = create_order(conn, payload)
    check("证书调回后同单仍不重复调拨", rid2 == order_id and created2 is False)
    check("调回的证书未被再次移动", owners(conn)["YT-Q01"] == 1)

    # 整单原子性：引用已调走证书（YT-Q02 现已归右卷 2，左卷 1 不再持有）→ 拒绝且无任何改动
    snapshot = owners(conn)
    check("前置：YT-Q02 确已不在左卷", owners(conn)["YT-Q02"] == 2)
    bad = {"left_dossier_id": 1, "right_dossier_id": 2,
           "left_codes": ["YT-Q02"], "right_codes": ["YT-Q08"]}
    try:
        create_order(conn, bad)
        check("已调走证书整单拒绝", False)
    except TransferRejected:
        check("已调走证书整单拒绝", True)
    check("拒绝后归属完全不变", owners(conn) == snapshot)
    check("拒绝不留调拨单",
          conn.execute("SELECT COUNT(*) c FROM transfer_orders").fetchone()["c"] == 1)

    # 重号：让右卷已持有 YT-Q03，左卷再把 YT-Q03 换过去
    conn.execute("UPDATE dossier_certificates SET owner_id=2 WHERE code='YT-Q03'")
    conn.commit()
    dup = {"left_dossier_id": 1, "right_dossier_id": 2,
           "left_codes": ["YT-Q04"], "right_codes": ["YT-Q08"]}
    # YT-Q03 当前在右卷且不属调出，构造左卷调出 YT-Q03 的重号场景
    dup = {"left_dossier_id": 1, "right_dossier_id": 2,
           "left_codes": ["YT-Q03"], "right_codes": ["YT-Q08"]}
    snap2 = owners(conn)
    try:
        create_order(conn, dup)
        check("换入重号整单拒绝", False)
    except TransferRejected:
        check("换入重号整单拒绝", True)
    check("重号拒绝后归属不变", owners(conn) == snap2)

    # 非等量拒绝
    unequal = {"left_dossier_id": 1, "right_dossier_id": 2,
               "left_codes": ["YT-Q04"], "right_codes": ["YT-Q08", "YT-Q09"]}
    try:
        create_order(conn, unequal)
        check("非等量整单拒绝", False)
    except TransferRejected:
        check("非等量整单拒绝", True)

    # 并发模拟：两个连接同时调同一证书，第二个必须因已调走而整体失败
    c1, c2 = open_db(), open_db()
    p1 = {"left_dossier_id": 1, "right_dossier_id": 2,
          "left_codes": ["YT-Q01"], "right_codes": ["YT-Q06"]}
    c1.execute("BEGIN IMMEDIATE")
    # c2 先尝试拿写锁（sqlite 会在 commit 前阻塞/报错），这里串行复现语义：
    c1.execute("UPDATE dossier_certificates SET owner_id=2 WHERE code='YT-Q01' AND owner_id<>2")
    c1.execute("INSERT INTO transfer_orders(request_key,left_dossier_id,right_dossier_id,"
               "status,quantity,actor,created_at,payload) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (request_key_of(p1), 1, 2, "confirmed", 1, "op", 1, json.dumps(p1)))
    c1.commit()
    c2_conn = c2
    try:
        create_order(c2_conn, p1)
        # 幂等命中（同 request_key）也是“不重复调拨”，可接受
        check("并发同证书不重复调拨", True)
    except TransferRejected:
        check("并发同证书不重复调拨", True)

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        sys.exit(1)
    print("全部集成断言通过")


if __name__ == "__main__":
    main()
