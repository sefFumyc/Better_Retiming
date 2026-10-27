# -*- coding: utf-8 -*-
"""编辑交互纯逻辑的单测（无 bpy）：双击检测 + 右键点目标守卫。

直接操纵（ADR-0005）后不存在常驻编辑会话，双击检测与"右键选中的点"的
状态都必须活在 operator 实例之外——这两块抽成纯类，在这里单测。
"""

import unittest

from seqvel import editing


class TestDoubleClick(unittest.TestCase):
    def test_two_fast_clicks_at_same_spot_insert(self):
        tracker = editing.DoubleClick()
        self.assertFalse(tracker.click("strip", (100.0, 90.0), now=10.0))
        self.assertTrue(tracker.click("strip", (102.0, 92.0), now=10.2),
                        "0.2s / 2px 后再点应判定为双击")

    def test_slow_second_click_is_not_a_double(self):
        tracker = editing.DoubleClick()
        tracker.click("strip", (100.0, 90.0), now=10.0)
        self.assertFalse(tracker.click("strip", (100.0, 90.0), now=10.6),
                         "超过窗口时间的第二次点击不算双击")

    def test_far_second_click_is_not_a_double(self):
        tracker = editing.DoubleClick()
        tracker.click("strip", (100.0, 90.0), now=10.0)
        self.assertFalse(tracker.click("strip", (130.0, 90.0), now=10.1),
                         "移出像素窗口的第二次点击不算双击")

    def test_click_on_another_strip_resets(self):
        tracker = editing.DoubleClick()
        tracker.click("strip_a", (100.0, 90.0), now=10.0)
        self.assertFalse(tracker.click("strip_b", (100.0, 90.0), now=10.1))

    def test_state_resets_after_a_double(self):
        """三连点 = 双击 + 新的第一次，不能连环插点。"""
        tracker = editing.DoubleClick()
        tracker.click("strip", (100.0, 90.0), now=10.0)
        self.assertTrue(tracker.click("strip", (100.0, 90.0), now=10.1))
        self.assertFalse(tracker.click("strip", (100.0, 90.0), now=10.2))

    def test_click_without_pixel_position_is_ignored(self):
        """没有鼠标坐标（按钮路径）不能参与双击，也不能留下半截状态。"""
        tracker = editing.DoubleClick()
        self.assertFalse(tracker.click("strip", None, now=10.0))
        self.assertFalse(tracker.click("strip", (100.0, 90.0), now=10.1))
        self.assertTrue(tracker.click("strip", (100.0, 90.0), now=10.2))

    def test_forget_clears_state(self):
        """拖动发生过就清状态：拖完的松手不能被算成双击的第一次。"""
        tracker = editing.DoubleClick()
        tracker.click("strip", (100.0, 90.0), now=10.0)
        tracker.forget()
        self.assertFalse(tracker.click("strip", (100.0, 90.0), now=10.1))


class TestPointTarget(unittest.TestCase):
    def test_fresh_target_is_usable(self):
        target = editing.PointTarget("strip", 1, made_at=100.0, ttl=15.0)
        self.assertEqual(target.use(now=110.0), ("strip", 1))

    def test_expired_target_is_refused(self):
        """目标过期（右键选了很久之后才删除）：绝不能删掉一个看不见的旧点。"""
        target = editing.PointTarget("strip", 1, made_at=100.0, ttl=15.0)
        self.assertIsNone(target.use(now=200.0))

    def test_missing_index_is_refused(self):
        target = editing.PointTarget("strip", None, made_at=100.0, ttl=15.0)
        self.assertIsNone(target.use(now=101.0))

    def test_zero_ttl_never_usable(self):
        target = editing.PointTarget("strip", 0, made_at=100.0, ttl=0.0)
        self.assertIsNone(target.use(now=100.0))


if __name__ == "__main__":
    unittest.main()
