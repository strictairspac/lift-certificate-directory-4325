"""证书调拨领域逻辑。

两份卷宗之间成批等量互换证书。该模块只保留纯函数与数据结构，不依赖
数据库，便于在没有外部服务的情况下重复验证业务口径：

* 同一份卷宗里的同一张证书只能调出一次；
* 任一证书已调走、换完后任一卷宗超登记上限或出现重号，整单不生效；
* 调拨确认前按旧归属点查，确认后按新归属点查。
"""

from dataclasses import dataclass, field
from datetime import date

from .certificates import Certificate
from .hash_directory import stable_hash


class TransferError(ValueError):
    """调拨单校验失败，整单不生效。"""


@dataclass(frozen=True)
class Dossier:
    """登记卷宗。capacity 是登记上限，满卷仍可等量换出换入。"""

    id: int
    code: str
    title: str
    capacity: int
    certificates: dict[str, Certificate] = field(hash=False)

    def snapshot_codes(self):
        return sorted(self.certificates)


@dataclass(frozen=True)
class TransferPlan:
    """一次已通过校验的等量互换计划。"""

    left_id: int
    right_id: int
    left_out: tuple[str, ...]
    right_out: tuple[str, ...]

    @property
    def quantity(self):
        return len(self.left_out)

    def legs(self):
        return ((self.left_id, self.right_id, self.left_out),
                (self.right_id, self.left_id, self.right_out))


def _unique_codes(codes, label):
    if not isinstance(codes, list) or not codes:
        raise TransferError(f"{label} 须为非空编号数组")
    normalized = []
    for code in codes:
        if not isinstance(code, str) or not code:
            raise TransferError(f"{label} 含空编号")
        normalized.append(code)
    if len(set(normalized)) != len(normalized):
        raise TransferError(f"{label} 出现重号，同一卷宗的同一张证书只能调出一次")
    return normalized


def parse_request(payload):
    """解析并校验调拨请求的形状，返回 (left_id, right_id, left_out, right_out)。"""
    if not isinstance(payload, dict):
        raise TransferError("调拨请求须为对象")
    try:
        left_id = int(payload["left_dossier_id"])
        right_id = int(payload["right_dossier_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransferError("须指定两侧卷宗编号") from exc
    if left_id == right_id:
        raise TransferError("两份卷宗之间才能互换证书")
    left_out = _unique_codes(payload.get("left_codes"), "调出证书")
    right_out = _unique_codes(payload.get("right_codes"), "调入证书")
    if len(left_out) != len(right_out):
        raise TransferError("成批互换须等量：两侧证书数量不一致")
    return left_id, right_id, tuple(left_out), tuple(right_out)


def build_plan(dossiers, payload):
    """对当前卷宗状态做整单校验，通过则返回可执行的互换计划。

    dossiers: {dossier_id: Dossier}，由服务层在事务内加锁后构造。
    任一前提不满足都抛 TransferError，调用方必须整体回滚。
    """
    left_id, right_id, left_out, right_out = parse_request(payload)
    left = dossiers.get(left_id)
    right = dossiers.get(right_id)
    if left is None or right is None:
        raise TransferError("指定的卷宗不存在")
    if set(left_out) & set(right_out):
        raise TransferError("同一编号不能在两侧同时调拨")

    # 每张调出证书必须仍登记在原卷宗，即未被并发调拨调走。
    for code in left_out:
        if code not in left.certificates:
            raise TransferError(f"证书 {code} 已不在调出卷宗，整单不生效")
    for code in right_out:
        if code not in right.certificates:
            raise TransferError(f"证书 {code} 已不在调出卷宗，整单不生效")

    # 换入后不得与卷宗内保留的证书重号（调出的已先移出，不会与换入冲突）。
    left_remaining = set(left.certificates) - set(left_out)
    right_remaining = set(right.certificates) - set(right_out)
    left_duplicates = set(right_out) & left_remaining
    right_duplicates = set(left_out) & right_remaining
    if left_duplicates:
        raise TransferError(f"换入后卷宗 {left.code} 出现重号：{sorted(left_duplicates)}")
    if right_duplicates:
        raise TransferError(f"换入后卷宗 {right.code} 出现重号：{sorted(right_duplicates)}")

    # 等量互换不改变在册数量；这里仍显式核对登记上限，覆盖非等量等边界。
    left_after = len(left_remaining) + len(right_out)
    right_after = len(right_remaining) + len(left_out)
    if left_after > left.capacity:
        raise TransferError(f"换入后卷宗 {left.code} 超出登记上限 {left.capacity}")
    if right_after > right.capacity:
        raise TransferError(f"换入后卷宗 {right.code} 超出登记上限 {right.capacity}")

    return TransferPlan(left_id, right_id, left_out, right_out)


def apply_plan(dossiers, plan):
    """在内存快照上执行互换，返回新的 {id: Dossier}（不修改入参）。"""
    moved = {}
    for source_id, target_id, codes in plan.legs():
        for code in codes:
            moved[(source_id, code)] = dossiers[source_id].certificates[code]

    result = {}
    for dossier_id, dossier in dossiers.items():
        records = dict(dossier.certificates)
        if dossier_id == plan.left_id:
            for code in plan.left_out:
                records.pop(code)
            for code in plan.right_out:
                records[code] = moved[(plan.right_id, code)]
        elif dossier_id == plan.right_id:
            for code in plan.right_out:
                records.pop(code)
            for code in plan.left_out:
                records[code] = moved[(plan.left_id, code)]
        result[dossier_id] = Dossier(
            dossier.id, dossier.code, dossier.title, dossier.capacity, records
        )
    return result


def point_check(dossiers, probes, review_date):
    """按当前归属对编号做点查，显式返回有效、到期、撤销与未登记。

    复用哈希目录的稳定寻址口径定位证书，结论与归属卷宗一并返回。
    """
    rows = []
    for code in probes:
        owner = None
        certificate = None
        bucket_no = None
        for dossier in dossiers.values():
            target = dossier.certificates.get(code)
            if target is not None:
                owner = dossier
                certificate = target
                bucket_no = stable_hash(code) & 0xFFFF
                break
        if certificate is None:
            verdict = "未登记"
            rows.append({
                "类别": "调拨点查", "项目": code, "归属卷宗": None,
                "电梯或槽位": None, "桶线索": None, "结论": verdict,
            })
            continue
        if certificate.revoked:
            verdict = "已撤销"
        elif review_date >= certificate.expires_on:
            verdict = "已到期"
        else:
            verdict = "有效"
        rows.append({
            "类别": "调拨点查", "项目": code, "归属卷宗": owner.code,
            "电梯或槽位": certificate.lift, "桶线索": bucket_no, "结论": verdict,
        })
    return rows
