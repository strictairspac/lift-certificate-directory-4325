"""卷宗证书调拨服务。

并发口径：
* 同一事务内按卷宗编号升序锁定两份卷宗，再锁定其在册证书行，
  同一份卷宗里的同一张证书在任一时刻只能被一个调拨单调走；
* request_key 是请求体的规范化哈希，落 transfer_orders 唯一键，
  同单重试直接返回已确认单据，即使证书后来又被调回原卷也不会重复调拨；
* 领域校验（已调走 / 超上限 / 重号）抛错即整体回滚，单据不生效。
"""

from datetime import date
from time import time

from fastapi import HTTPException
from sqlalchemy import and_, insert, select
from sqlalchemy.exc import IntegrityError

from src.domain.certificates import Certificate
from src.domain.transfer import (
    Dossier,
    TransferError,
    build_plan,
    parse_request,
    point_check as domain_point_check,
)
from src.models.transfers import (
    dossier_certificates,
    dossiers,
    transfer_order_items,
    transfer_orders,
)
from src.utils.serial import content_digest
from . import audit


def request_key_of(payload):
    """同一份调拨请求（含两侧编号及调出列表）得到稳定幂等键。"""
    return content_digest({"transfer": payload})[:64]


def _lock_dossiers(db, left_id, right_id):
    ids = sorted({left_id, right_id})
    rows = db.execute(
        select(dossiers).where(dossiers.c.id.in_(ids)).order_by(dossiers.c.id).with_for_update()
    ).mappings().all()
    if len(rows) != len(ids):
        raise HTTPException(404, "指定的卷宗不存在")
    return {row["id"]: dict(row) for row in rows}


def _load_certificates(db, dossier_ids):
    rows = db.execute(
        select(dossier_certificates).where(dossier_certificates.c.owner_id.in_(dossier_ids))
        .order_by(dossier_certificates.c.id).with_for_update()
    ).mappings().all()
    grouped = {dossier_id: {} for dossier_id in dossier_ids}
    for row in rows:
        grouped[row["owner_id"]][row["code"]] = Certificate(
            code=row["code"], lift=row["lift"],
            expires_on=date.fromisoformat(row["expires_on"]),
            revoked=row["revoked"] == "1",
        )
    return grouped


def _build_domain(dossier_rows, grouped):
    return {
        dossier_id: Dossier(
            id=dossier_id, code=row["code"], title=row["title"],
            capacity=row["capacity"], certificates=grouped.get(dossier_id, {}),
        )
        for dossier_id, row in dossier_rows.items()
    }


def _items(db, order_id):
    return [dict(row) for row in db.execute(
        select(transfer_order_items).where(transfer_order_items.c.order_id == order_id)
        .order_by(transfer_order_items.c.id)
    ).mappings()]


def _order_payload(db, order):
    data = dict(order)
    data["items"] = _items(db, order["id"])
    return data


def list_orders(db):
    orders = [dict(row) for row in db.execute(
        select(transfer_orders).order_by(transfer_orders.c.id.desc()).limit(100)
    ).mappings()]
    return [{**order, "items": _items(db, order["id"])} for order in orders]


def get_order(db, order_id):
    order = db.execute(
        select(transfer_orders).where(transfer_orders.c.id == order_id)
    ).mappings().first()
    if order is None:
        raise HTTPException(404, "调拨单不存在")
    return _order_payload(db, order)


def create_order(db, payload, actor):
    # 先做无副作用的形状校验，错误请求不落任何数据。
    left_id, right_id, left_out, right_out = parse_request(payload)
    key = request_key_of(payload)

    existing = db.execute(
        select(transfer_orders).where(transfer_orders.c.request_key == key).with_for_update()
    ).mappings().first()
    if existing is not None:
        # 同单重试：不重复调拨，直接返回已确认单据（即便证书后来调回原卷）。
        return _order_payload(db, existing), False

    # 锁定卷宗与证书行：并发调拨在此串行化，同一证书不可能被调两次。
    dossier_rows = _lock_dossiers(db, left_id, right_id)
    grouped = _load_certificates(db, list(dossier_rows))

    # 拿到锁后再查一次幂等键：并发的同单请求在前者提交后会在此命中原单，
    # 而不是带着已变更的归属走到校验失败，确保同单绝不重复调拨。
    committed = db.execute(
        select(transfer_orders).where(transfer_orders.c.request_key == key)
    ).mappings().first()
    if committed is not None:
        return _order_payload(db, committed), False

    domain = _build_domain(dossier_rows, grouped)

    try:
        plan = build_plan(domain, payload)
    except TransferError as exc:
        # 整单不生效：上层回滚事务，不保留任何调拨痕迹。
        raise HTTPException(422, str(exc)) from exc

    try:
        change = db.execute(insert(transfer_orders).values(
            request_key=key, left_dossier_id=left_id, right_dossier_id=right_id,
            status="confirmed", quantity=plan.quantity, actor=actor,
            created_at=int(time()), payload=payload,
        ))
    except IntegrityError as exc:
        # 相同卷宗对在前面的 FOR UPDATE 上已串行化，正常重试走 existing 分支。
        # 走到这里说明出现极端唯一键冲突；当前事务已中止，干净地报冲突由整单回滚处理。
        raise HTTPException(409, "调拨单已存在或正在处理，请勿重复提交") from exc
    order_id = change.inserted_primary_key[0]

    item_rows = [
        {"order_id": order_id, "certificate_code": code,
         "source_dossier_id": source, "target_dossier_id": target}
        for source, target, codes in plan.legs() for code in codes
    ]
    if item_rows:
        db.execute(insert(transfer_order_items), item_rows)

    # 仅改归属：把每张调出证书的 owner 指向目标卷宗。
    moved = [(code, target) for _source, target, codes in plan.legs() for code in codes]
    for code, target in moved:
        db.execute(
            dossier_certificates.update()
            .where(and_(dossier_certificates.c.code == code,
                        dossier_certificates.c.owner_id != target))
            .values(owner_id=target)
        )

    audit.record(db, "transfer", actor, "transfer_order", order_id)
    return get_order(db, order_id), True


def list_dossiers(db):
    rows = db.execute(select(dossiers).order_by(dossiers.c.id)).mappings().all()
    result = []
    for row in rows:
        entry = dict(row)
        entry["certificates"] = [dict(item) for item in db.execute(
            select(dossier_certificates.c.code, dossier_certificates.c.lift,
                   dossier_certificates.c.expires_on, dossier_certificates.c.revoked)
            .where(dossier_certificates.c.owner_id == row["id"])
            .order_by(dossier_certificates.c.code)
        ).mappings()]
        result.append(entry)
    return result


def current_dossiers(db, dossier_ids=None):
    """构造当前归属的领域卷宗（不加锁，供只读点查/检证）。"""
    condition = dossiers.c.id.in_(dossier_ids) if dossier_ids else None
    rows = db.execute(select(dossiers).where(condition).order_by(dossiers.c.id)
                      if condition is not None else select(dossiers).order_by(dossiers.c.id)
                      ).mappings().all()
    ids = [row["id"] for row in rows]
    grouped = {dossier_id: {} for dossier_id in ids}
    if ids:
        cert_rows = db.execute(
            select(dossier_certificates).where(dossier_certificates.c.owner_id.in_(ids))
        ).mappings().all()
        for row in cert_rows:
            grouped[row["owner_id"]][row["code"]] = Certificate(
                code=row["code"], lift=row["lift"],
                expires_on=date.fromisoformat(row["expires_on"]),
                revoked=row["revoked"] == "1",
            )
    return _build_domain({row["id"]: dict(row) for row in rows}, grouped)


def point_check(db, probes, review_date):
    """按当前归属点查编号；调拨确认前是旧归属，确认后是新归属。"""
    if not isinstance(probes, list) or not 1 <= len(probes) <= 100:
        raise HTTPException(422, "检证编号须为 1–100 条")
    if not all(isinstance(code, str) and code for code in probes):
        raise HTTPException(422, "检证编号须为非空字符串")
    domain = current_dossiers(db)
    return domain_point_check(domain, probes, review_date)


def verify_order(db, order_id, review_date):
    """检证页面：对调拨单涉及的全部证书按确认后的新归属出结论。"""
    order = get_order(db, order_id)
    domain = current_dossiers(db, [order["left_dossier_id"], order["right_dossier_id"]])
    codes = [*order["payload"]["left_codes"], *order["payload"]["right_codes"]]
    rows = domain_point_check(domain, codes, review_date)
    return {
        "order_id": order["id"], "request_key": order["request_key"],
        "status": order["status"], "quantity": order["quantity"],
        "left_dossier_id": order["left_dossier_id"],
        "right_dossier_id": order["right_dossier_id"], "rows": rows,
    }
