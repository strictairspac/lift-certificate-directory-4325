"""调拨领域逻辑测试：整单原子性、等量互换、重号/上限、点查归属切换。

仅依赖标准库，运行：
    PYTHONPATH=. python3 -m unittest discover -s tests -v
"""

import unittest
from datetime import date

from src.domain.certificates import Certificate
from src.domain.transfer import (
    Dossier,
    TransferError,
    apply_plan,
    build_plan,
    parse_request,
    point_check,
)


def cert(code, expires="2026-12-31", revoked=False):
    return Certificate(f"YT-Q{code:02d}", f"YT-L{code:02d}", date.fromisoformat(expires), revoked)


def dossier(id_, code, capacity, codes):
    return Dossier(id_, code, f"卷宗{id_}", capacity, {c: cert(int(c[4:])) for c in codes})


def swap(left, right, n=1):
    return {
        "left_dossier_id": left, "right_dossier_id": right,
        "left_codes": [f"YT-Q0{i}" for i in range(1, n + 1)],
        "right_codes": [f"YT-Q0{i}" for i in range(5, 5 + n)],
    }


class TransferDomainTest(unittest.TestCase):
    def setUp(self):
        # 两卷各 4 件、上限 4（满卷），用于验证满卷也能等量换。
        self.world = {
            1: dossier(1, "DOS-A", 4, ["YT-Q01", "YT-Q02", "YT-Q03", "YT-Q04"]),
            2: dossier(2, "DOS-B", 4, ["YT-Q05", "YT-Q06", "YT-Q07", "YT-Q08"]),
        }

    def test_full_dossiers_swap_equal_quantity(self):
        plan = build_plan(self.world, swap(1, 2, n=2))
        after = apply_plan(self.world, plan)
        self.assertEqual(set(after[1].certificates),
                         {"YT-Q03", "YT-Q04", "YT-Q05", "YT-Q06"})
        self.assertEqual(set(after[2].certificates),
                         {"YT-Q01", "YT-Q02", "YT-Q07", "YT-Q08"})
        # 入参快照不被修改
        self.assertIn("YT-Q01", self.world[1].certificates)
        self.assertEqual(plan.quantity, 2)

    def test_unequal_quantity_rejected(self):
        payload = swap(1, 2)
        payload["right_codes"] = ["YT-Q05", "YT-Q06"]
        with self.assertRaisesRegex(TransferError, "等量"):
            build_plan(self.world, payload)

    def test_duplicate_within_same_side_rejected(self):
        payload = swap(1, 2)
        payload["left_codes"] = ["YT-Q01", "YT-Q01"]
        with self.assertRaisesRegex(TransferError, "重号"):
            parse_request(payload)

    def test_certificate_already_moved_rejects_whole_order(self):
        # 模拟 YT-Q01 已被并发调走：卷宗 1 不再持有它。
        self.world[1].certificates.pop("YT-Q01")
        with self.assertRaisesRegex(TransferError, "已不在调出卷宗"):
            build_plan(self.world, swap(1, 2))

    def test_incoming_duplicate_number_rejects_whole_order(self):
        # 卷宗 2 已持有 YT-Q03，卷宗 1 又把 YT-Q03 换入卷宗 2 → 重号。
        self.world[2] = Dossier(2, "DOS-B", "卷宗2", 8,
                                {**dict(self.world[2].certificates), "YT-Q03": cert(3)})
        payload = {
            "left_dossier_id": 1, "right_dossier_id": 2,
            "left_codes": ["YT-Q03"], "right_codes": ["YT-Q05"],
        }
        with self.assertRaisesRegex(TransferError, "重号"):
            build_plan(self.world, payload)

    def test_capacity_overflow_rejects_whole_order(self):
        # 卷宗 1 当前 2 件但登记上限被收紧为 1，等量换入后仍 2 件 → 超上限。
        small = Dossier(1, "DOS-A", "卷宗1", 1, {c: cert(int(c[4:]))
                                                 for c in ["YT-Q01", "YT-Q02"]})
        big = dossier(2, "DOS-B", 4, ["YT-Q03", "YT-Q04", "YT-Q05", "YT-Q06"])
        payload = {
            "left_dossier_id": 1, "right_dossier_id": 2,
            "left_codes": ["YT-Q01", "YT-Q02"],
            "right_codes": ["YT-Q03", "YT-Q04"],
        }
        with self.assertRaisesRegex(TransferError, "登记上限"):
            build_plan({1: small, 2: big}, payload)

    def test_same_dossier_rejected(self):
        with self.assertRaisesRegex(TransferError, "两份卷宗"):
            build_plan(self.world, {**swap(1, 2), "right_dossier_id": 1})

    def test_missing_dossier_rejected(self):
        with self.assertRaisesRegex(TransferError, "卷宗不存在"):
            build_plan(self.world, {**swap(1, 2), "right_dossier_id": 99})

    def test_cross_listing_same_code_rejected(self):
        payload = {
            "left_dossier_id": 1, "right_dossier_id": 2,
            "left_codes": ["YT-Q01"], "right_codes": ["YT-Q01"],
        }
        with self.assertRaisesRegex(TransferError, "不能在两侧同时调拨"):
            build_plan(self.world, payload)

    def test_point_check_ownership_before_and_after_confirmation(self):
        review = date(2026, 11, 25)
        plan = build_plan(self.world, swap(1, 2))
        # 确认前：YT-Q01 仍归卷宗 1
        before = {row["项目"]: row for row in point_check(self.world, ["YT-Q01"], review)}
        self.assertEqual(before["YT-Q01"]["归属卷宗"], "DOS-A")
        self.assertEqual(before["YT-Q01"]["结论"], "有效")
        after_world = apply_plan(self.world, plan)
        # 确认后：YT-Q01 归卷宗 2
        after = {row["项目"]: row for row in point_check(after_world, ["YT-Q01"], review)}
        self.assertEqual(after["YT-Q01"]["归属卷宗"], "DOS-B")

    def test_point_check_unregistered_and_status(self):
        review = date(2026, 11, 25)
        self.world[1].certificates["YT-Q09"] = cert(9, expires="2026-11-20")  # 已到期
        self.world[1].certificates["YT-Q10"] = Certificate(
            "YT-Q10", "YT-L10", date(2026, 12, 31), True)  # 已撤销
        rows = {row["项目"]: row for row in
                point_check(self.world, ["YT-Q99", "YT-Q09", "YT-Q10"], review)}
        self.assertEqual(rows["YT-Q99"]["结论"], "未登记")
        self.assertEqual(rows["YT-Q09"]["结论"], "已到期")
        self.assertEqual(rows["YT-Q10"]["结论"], "已撤销")

    def test_same_request_builds_identical_plan(self):
        # 同一请求体多次构造得到完全一致、确定的计划（服务层据此做幂等）。
        self.assertEqual(build_plan(self.world, swap(1, 2)),
                         build_plan(self.world, swap(1, 2)))


if __name__ == "__main__":
    unittest.main()
