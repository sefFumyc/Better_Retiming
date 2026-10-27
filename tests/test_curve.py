# -*- coding: utf-8 -*-
"""接缝 1：速度曲线影子模型的单测（纯 Python，零 bpy）。"""

import inspect
import random
import unittest

from seqvel.curve import (
    CURVE_TYPES,
    FAST,
    LINEAR,
    MIN_GAP,
    MIN_POINTS,
    SLOW,
    SMOOTH,
    SPEED_CEIL,
    SPEED_MAX,
    SPEED_MIN,
    TYPE_LABELS,
    Curve,
    clamp_speed,
    speed_to_yfrac,
    to_abs_frame,
    to_rel_frame,
    yfrac_to_speed,
    yfrac_to_speed_raw,
)


class TestScaling(unittest.TestCase):
    def test_speed_clamped_into_range(self):
        # v1.5.0：缺省钳制走**数据硬顶** SPEED_CEIL（显示上限只管显示/拖动）
        self.assertEqual(clamp_speed(-5.0), SPEED_MIN)
        self.assertEqual(clamp_speed(0.0), SPEED_MIN)
        self.assertEqual(clamp_speed(99.0), SPEED_CEIL)
        self.assertEqual(clamp_speed(1.0), 1.0)

    def test_speed_clamped_tolerates_junk(self):
        self.assertEqual(clamp_speed("x"), 1.0)
        self.assertEqual(clamp_speed(None), 1.0)
        self.assertEqual(clamp_speed(float("nan")), 1.0)
        self.assertEqual(clamp_speed(float("inf")), 1.0)

    def test_speed_max_is_three_x(self):
        self.assertEqual(SPEED_MAX, 3.0)

    def test_one_x_sits_at_one_third_of_the_ruler(self):
        # 标尺跨通道高的 9%~95%（跨度 0.86：Blender 绘制区 5%~95% 的下沿再留
        # 4% 底部留白，ADR-0010 修订 R1）
        # ⇒ 1.0x 落在标尺 1/3 处 = 0.09 + 0.86/3 ≈ 0.3767（不是通道的 1/3）
        self.assertAlmostEqual(speed_to_yfrac(1.0), 0.09 + 0.86 / 3.0)
        self.assertAlmostEqual(speed_to_yfrac(SPEED_MIN),
                               0.09 + 0.86 * SPEED_MIN / SPEED_MAX)  # 最低速离素材下缘 4%
        self.assertAlmostEqual(speed_to_yfrac(SPEED_MAX), 0.95)  # 上限贴素材上缘

    def test_bottom_margin_is_four_percent_of_the_channel_height(self):
        """底部留白 = 4% 通道高（ADR-0010 修订 R1）：标尺的**零点**比 Blender
        绘制区下沿（0.05）高 0.04，顶端**不放**——贴到上缘仍然是"到达上限"。

        判据走**未钳**的逆映射：``speed_to_yfrac(0.0)`` 测到的其实是"0 被钳到
        ``SPEED_MIN``"，跟留白无关；``yfrac_to_speed_raw`` 不做钳制，才看得见
        零点落在哪、以及绘制区下沿对应的是负速度。
        """
        self.assertAlmostEqual(yfrac_to_speed_raw(0.09), 0.0)
        self.assertAlmostEqual(yfrac_to_speed_raw(0.05), -0.04 / 0.86 * SPEED_MAX)
        self.assertAlmostEqual(speed_to_yfrac(SPEED_MAX), 0.95)

    def test_vertical_mapping_round_trip(self):
        for speed in (0.02, 0.5, 1.0, 1.37, 2.0, 3.0):
            self.assertAlmostEqual(yfrac_to_speed(speed_to_yfrac(speed)), speed, places=5)

    def test_vertical_mapping_clamps(self):
        self.assertEqual(yfrac_to_speed(-0.4), SPEED_MIN)
        self.assertEqual(yfrac_to_speed(3.0), SPEED_MAX)

    def test_frame_conversion_rounds_to_whole_frames(self):
        self.assertEqual(to_abs_frame(1, 0.4), 1)
        self.assertEqual(to_abs_frame(100, 20.6), 121)
        self.assertAlmostEqual(to_rel_frame(1, 121), 120.0)


class TestConstruction(unittest.TestCase):
    def test_default_is_two_points_at_one_x(self):
        curve = Curve.default(120)
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve.points, [(0.0, 1.0), (120.0, 1.0)])
        self.assertTrue(curve.is_valid())

    def test_first_point_frame_pinned_to_zero(self):
        curve = Curve([(30.0, 1.5), (60.0, 0.5)], 60)
        self.assertEqual(curve.frames()[0], 0.0)
        self.assertEqual(curve.points[0][1], 1.5)

    def test_points_are_sorted_and_speed_clamped(self):
        # v1.5.0：构造读回钳到**数据硬顶** SPEED_CEIL（10.0），不吃显示上限
        curve = Curve([(80.0, 99.0), (10.0, 0.0), (40.0, 1.0)], 100)
        self.assertEqual(curve.frames(), [0.0, 40.0, 80.0])
        self.assertEqual(curve.speeds(), [SPEED_MIN, 1.0, SPEED_CEIL])
        self.assertTrue(all(SPEED_MIN <= s <= SPEED_CEIL for s in curve.speeds()))

    def test_junk_input_falls_back_to_default(self):
        curve = Curve([("a", "b"), (None, None), ("c", "d")], 50)
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.0)])

    def test_single_point_is_padded_to_two(self):
        curve = Curve([(0.0, 1.7)], 30)
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve.points[1], (30.0, 1.7))

    def test_legacy_curve_data_is_untouched_by_new_limit(self):
        """上限 2.0 → 3.0 只放宽钳制：旧 .blend 里存的点逐点不变。"""
        curve = Curve.from_flat([0.0, 1.0, 40.0, 2.0, 90.0, 0.5], 90.0)
        self.assertEqual(curve.points, [(0.0, 1.0), (40.0, 2.0), (90.0, 0.5)])

    def test_flat_round_trip(self):
        source = Curve([(0.0, 1.0), (40.0, 1.5), (120.5, 0.25)], 120)
        restored = Curve.from_flat(source.to_flat(), 120)
        self.assertEqual(restored.points, source.points)

    def test_from_flat_rejects_broken_payload(self):
        self.assertEqual(Curve.from_flat([], 60).points, Curve.default(60).points)
        self.assertEqual(Curve.from_flat([0.0], 60).points, Curve.default(60).points)
        self.assertEqual(Curve.from_flat(["a", "b", "c", "d"], 60).points, Curve.default(60).points)
        sentinel = Curve.default(60)
        self.assertIs(Curve.from_flat(None, 60, fallback=sentinel), sentinel)

    def test_copy_is_independent(self):
        curve = Curve.default(100)
        clone = curve.copy()
        clone.move_point(1, rel_frame=50.0)
        self.assertNotEqual(clone.points, curve.points)
        self.assertEqual(curve.points[-1][0], 100.0)


class TestLineSpeed(unittest.TestCase):
    def setUp(self):
        self.curve = Curve([(0.0, 1.0), (40.0, 2.0), (100.0, 1.0)], 100)

    def test_interpolates_within_segment(self):
        self.assertAlmostEqual(self.curve.line_speed_at(20), 1.5)
        self.assertAlmostEqual(self.curve.line_speed_at(70), 1.5)
        self.assertAlmostEqual(self.curve.line_speed_at(60), 1.0 + (2.0 - 1.0) * (1 - (60 - 40) / 60.0))

    def test_on_point_returns_point_speed(self):
        for frame, speed in self.curve.points:
            self.assertAlmostEqual(self.curve.line_speed_at(frame), speed)

    def test_extrapolates_flat_beyond_both_ends(self):
        self.assertAlmostEqual(self.curve.line_speed_at(-50), 1.0)
        self.assertAlmostEqual(self.curve.line_speed_at(500), 1.0)

    def test_flat_segment_stays_flat(self):
        curve = Curve([(0.0, 1.3), (50.0, 1.3), (100.0, 1.3)], 100)
        self.assertAlmostEqual(curve.line_speed_at(75), 1.3)

    def test_same_frame_pair_behaves_as_hard_cut(self):
        curve = Curve([(0.0, 1.0), (50.0, 0.5), (50.0, 1.8), (100.0, 1.8)], 100)
        self.assertAlmostEqual(curve.line_speed_at(50.0), 0.5)
        self.assertAlmostEqual(curve.line_speed_at(51.0), 1.8)


class TestInsertDelete(unittest.TestCase):
    def test_insert_keeps_sorted_and_takes_line_value(self):
        curve = Curve([(0.0, 1.0), (100.0, 2.0)], 100)
        index = curve.insert_point(50.0)
        self.assertEqual(index, 1)
        self.assertEqual(curve.frames(), [0.0, 50.0, 100.0])
        self.assertAlmostEqual(curve.points[1][1], 1.5)

    def test_insert_accepts_explicit_speed_clamped(self):
        # v1.5.0：显式速度钳到数据硬顶（插点初值=线上值，显式 speed 仅 API 用）
        curve = Curve.default(100)
        curve.insert_point(50.0, speed=99.0)
        self.assertEqual(curve.points[1][1], SPEED_CEIL)

    def test_insert_rejects_too_close_to_existing(self):
        curve = Curve([(0.0, 1.0), (100.0, 1.0)], 100)
        self.assertIsNone(curve.insert_point(0.2))
        self.assertIsNone(curve.insert_point(100.0 - MIN_GAP / 2))
        self.assertIsNone(curve.insert_point(100.0))
        self.assertEqual(len(curve), 2)

    def test_insert_clamps_into_strip_range(self):
        curve = Curve.default(100)
        curve.move_point(1, rel_frame=60.0)
        curve.insert_point(900.0)  # 钳到条带末尾
        curve.insert_point(-30.0)  # 钳到条带开头 → 与首点重合 → 拒绝
        self.assertEqual(curve.frames(), [0.0, 60.0, 100.0])

    def test_delete_protects_first_point(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 1.0)], 100)
        self.assertFalse(curve.can_delete(0))
        self.assertFalse(curve.delete_point(0))
        self.assertEqual(len(curve), 3)

    def test_delete_keeps_at_least_two_points(self):
        curve = Curve.default(100)
        self.assertFalse(curve.delete_point(1))
        self.assertEqual(len(curve), MIN_POINTS)

    def test_delete_removes_middle_and_tail_points(self):
        curve = Curve([(0.0, 1.0), (40.0, 2.0), (80.0, 0.5), (100.0, 1.0)], 100)
        self.assertTrue(curve.delete_point(1))
        self.assertEqual(curve.frames(), [0.0, 80.0, 100.0])
        self.assertTrue(curve.delete_point(2))
        self.assertEqual(curve.frames(), [0.0, 80.0])
        self.assertTrue(curve.is_valid())

    def test_delete_out_of_range_is_rejected(self):
        curve = Curve.default(100)
        self.assertFalse(curve.delete_point(99))
        self.assertFalse(curve.delete_point(-1))


class TestMovePoint(unittest.TestCase):
    def setUp(self):
        self.curve = Curve([(0.0, 1.0), (50.0, 1.0), (100.0, 1.0)], 100)

    def test_first_point_x_is_fixed(self):
        self.curve.move_point(0, rel_frame=42.0, speed=1.5)
        self.assertEqual(self.curve.frames()[0], 0.0)
        self.assertAlmostEqual(self.curve.points[0][1], 1.5)

    def test_x_clamped_between_neighbours(self):
        self.curve.move_point(1, rel_frame=-20.0)
        self.assertAlmostEqual(self.curve.points[1][0], 0.0)
        self.curve.move_point(1, rel_frame=900.0)
        self.assertAlmostEqual(self.curve.points[1][0], 100.0)

    def test_last_point_can_move_left_and_keeps_tail_extension(self):
        curve = Curve.default(100)
        curve.move_point(1, rel_frame=35.0)
        self.assertAlmostEqual(curve.points[1][0], 35.0)
        self.assertEqual(curve.tail_extension_end(), 100.0)
        self.assertAlmostEqual(curve.line_speed_at(80.0), curve.points[1][1])

    def test_last_point_hidden_behind_previous_point(self):
        """末点被前一个点夹住时无法再左移（相邻点钳制优先）。"""
        self.curve.move_point(2, rel_frame=5.0)
        self.assertAlmostEqual(self.curve.points[2][0], 50.0)

    def test_no_tail_extension_when_last_point_at_end(self):
        self.assertIsNone(self.curve.tail_extension_end())

    def test_speed_clamped(self):
        # v1.5.0：move_point 的速度钳制走数据硬顶（光标路径已被映射层钳到显示上限）
        self.curve.move_point(1, speed=100.0)
        self.assertEqual(self.curve.points[1][1], SPEED_CEIL)
        self.curve.move_point(1, speed=-100.0)
        self.assertEqual(self.curve.points[1][1], SPEED_MIN)

    def test_no_change_reports_false(self):
        self.assertFalse(self.curve.move_point(1, rel_frame=50.0, speed=1.0))
        self.assertTrue(self.curve.move_point(1, speed=1.2))
        self.assertFalse(self.curve.move_point(9, speed=1.2))


class TestSegmentDrag(unittest.TestCase):
    """段拖动快照数学：回归"线比鼠标快"与"钳制后只能向下"两个历史 bug。"""

    def test_drag_moves_only_that_segment(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.0), (100.0, 1.0)], 100)
        drag = curve.begin_segment_drag(1, grab_frame=75.0)
        self.assertTrue(drag.apply(1.4))
        self.assertEqual(curve.speeds(), [1.0, 1.4, 1.4])

    def test_grab_point_follows_cursor_one_to_one(self):
        curve = Curve([(0.0, 0.6), (100.0, 1.4)], 100)
        drag = curve.begin_segment_drag(0, grab_frame=50.0)
        self.assertAlmostEqual(drag.anchor_speed, 1.0)
        drag.apply(1.3)
        self.assertAlmostEqual(curve.line_speed_at(50.0), 1.3, places=6)
        drag.apply(0.7)
        self.assertAlmostEqual(curve.line_speed_at(50.0), 0.7, places=6)

    def test_clamping_breaks_tracking_but_recovers_on_the_way_back(self):
        """贴边时抓取点不再跟手（叠加 bug 时代是"整条线都失控"），回程立即恢复。"""
        curve = Curve([(0.0, 2.0), (100.0, 2.6)], 100)
        drag = curve.begin_segment_drag(0, grab_frame=50.0, cap=SPEED_MAX)  # 抓取处 2.3
        drag.apply(2.8)  # 上端点被钳到 SPEED_MAX
        self.assertAlmostEqual(curve.speeds()[1], SPEED_MAX)
        self.assertAlmostEqual(curve.line_speed_at(50.0), 2.75, places=6)
        drag.apply(1.8)  # 回到值域内 → 立刻精确跟手
        self.assertAlmostEqual(curve.line_speed_at(50.0), 1.8, places=6)

    def test_reverse_restores_exactly(self):
        curve = Curve([(0.0, 1.2), (60.0, 0.8), (100.0, 1.0)], 100)
        original = list(curve.points)
        drag = curve.begin_segment_drag(0, grab_frame=20.0)
        drag.apply(1.9)
        drag.apply(0.4)
        drag.apply(drag.anchor_speed)
        self.assertEqual(curve.points, original)

    def test_repeated_apply_is_idempotent(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.0), (100.0, 1.0)], 100)
        drag = curve.begin_segment_drag(0, grab_frame=25.0)
        drag.apply(1.6)
        once = list(curve.points)
        drag.apply(1.6)
        self.assertEqual(curve.points, once)

    def test_clamped_drag_does_not_stick(self):
        """历史 bug：在钳制后的当前值上继续叠加 → 线比鼠标快且回不去。"""
        curve = Curve([(0.0, 1.0), (100.0, 1.0)], 100)
        drag = curve.begin_segment_drag(0, grab_frame=50.0, cap=SPEED_MAX)
        drag.apply(4.5)  # 越界 → 钳到 SPEED_MAX
        self.assertEqual(curve.speeds(), [SPEED_MAX, SPEED_MAX])
        drag.apply(2.0)  # 若叠加实现会得到 3.0 + 1.0 = 3.0（粘住）
        self.assertAlmostEqual(curve.speeds()[0], 2.0, places=5)
        self.assertAlmostEqual(curve.speeds()[1], 2.0, places=5)
        drag.apply(1.0)  # 反向回到快照
        self.assertEqual(curve.speeds(), [1.0, 1.0])

    def test_bottom_clamp_does_not_stick(self):
        curve = Curve([(0.0, 1.0), (60.0, 1.0)], 60)
        drag = curve.begin_segment_drag(0, grab_frame=10.0)
        drag.apply(-3.0)
        self.assertAlmostEqual(curve.speeds()[0], SPEED_MIN, places=6)
        self.assertAlmostEqual(curve.speeds()[1], SPEED_MIN, places=6)
        drag.apply(0.8)
        self.assertAlmostEqual(curve.speeds()[0], 0.8, places=6)

    def test_tail_drag_moves_only_last_point(self):
        curve = Curve([(0.0, 1.0), (40.0, 1.0), (100.0, 1.0)], 100)
        curve.move_point(2, rel_frame=60.0)
        drag = curve.begin_tail_drag(grab_frame=85.0)
        drag.apply(0.6)
        self.assertEqual(curve.speeds(), [1.0, 1.0, 0.6])
        self.assertEqual(curve.frames(), [0.0, 40.0, 60.0])

    def test_segment_drag_rejects_bad_index(self):
        curve = Curve.default(50)
        with self.assertRaises(IndexError):
            curve.begin_segment_drag(1, grab_frame=10.0)


class TestEndCarry(unittest.TestCase):
    """末端跟随 / 吞点（ADR-0004）：素材右端收短越过点时的模型行为。"""

    def test_carry_moves_only_last_point_when_end_crosses_it(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 0.8)], 100)
        self.assertTrue(curve.carry_tail(80.0))
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.5), (80.0, 0.8)])
        self.assertTrue(curve.is_valid())

    def test_carry_is_noop_when_end_moves_right_or_stays(self):
        """拉长不带动：末点原地不动，尾段水平延长线自然变长。"""
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 0.8)], 100)
        self.assertFalse(curve.carry_tail(120.0))
        self.assertFalse(curve.carry_tail(100.0))
        self.assertEqual(curve.points[-1], (100.0, 0.8))

    def test_swallow_removes_follower_not_blocker(self):
        """撞上倒数第二点：删跟随中的末点，升格者原速接管（左侧无跳变）。"""
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 0.8)], 100)
        self.assertTrue(curve.carry_tail(50.0))
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.5)])
        curve.set_duration(50.0)  # 右端就在 50：不再有延长线
        self.assertIsNone(curve.tail_extension_end())

    def test_two_point_floor_follows_unconditionally(self):
        curve = Curve([(0.0, 1.0), (100.0, 0.8)], 100)
        self.assertTrue(curve.carry_tail(30.0))
        self.assertEqual(curve.points, [(0.0, 1.0), (30.0, 0.8)])

    def test_swallow_multiple_points_in_one_step(self):
        """快速拖动一帧跨过多个点：连续吞掉、升格者接管，行为一致。"""
        curve = Curve([(0.0, 1.0), (30.0, 2.0), (60.0, 1.0), (90.0, 0.5)], 90)
        self.assertTrue(curve.carry_tail(20.0))
        self.assertEqual(curve.points, [(0.0, 1.0), (20.0, 2.0)])

    def test_carry_tail_clamps_at_zero(self):
        curve = Curve([(0.0, 1.0), (40.0, 1.0)], 40)
        self.assertTrue(curve.carry_tail(-5.0))
        self.assertEqual(curve.points, [(0.0, 1.0), (0.0, 1.0)])
        self.assertTrue(curve.is_valid())

    def test_carry_tail_rejects_junk(self):
        curve = Curve.default(50)
        self.assertFalse(curve.carry_tail(None))
        self.assertFalse(curve.carry_tail("x"))
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.0)])


class TestLegacyHeal(unittest.TestCase):
    """读取路径（构造 / from_flat）把越界末点静默钳回条带内（ADR-0004）。"""

    def test_constructor_clamps_tail_beyond_duration(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (120.0, 0.8)], 80)
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.5), (80.0, 0.8)])

    def test_constructor_swallows_points_beyond_duration(self):
        curve = Curve([(0.0, 1.0), (90.0, 2.0), (120.0, 0.8)], 80)
        self.assertEqual(curve.points, [(0.0, 1.0), (80.0, 2.0)])

    def test_constructor_with_zero_duration_touches_nothing(self):
        """时长读数不可信（异常退化 0）时绝不动数据。"""
        curve = Curve([(0.0, 1.0), (100.0, 0.8)], 0)
        self.assertEqual(curve.points, [(0.0, 1.0), (100.0, 0.8)])

    def test_from_flat_heals_legacy_payload(self):
        curve = Curve.from_flat([0.0, 1.0, 50.0, 1.5, 120.0, 0.8], 80)
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.5), (80.0, 0.8)])

    def test_set_duration_still_only_widens_clamp_range(self):
        """set_duration 刻意不带动点：左端裁剪 / 缓存命中路径行为不变（ADR-0004）。"""
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 0.8)], 100)
        curve.set_duration(40)
        self.assertEqual(curve.points, [(0.0, 1.0), (50.0, 1.5), (100.0, 0.8)])
        self.assertEqual(curve.duration, 40.0)


class TestSegmentTypes(unittest.TestCase):
    """段曲线类型：类型挂在段上、只改形状不改端点（grilling 定稿）。"""

    def test_default_types_are_all_linear(self):
        curve = Curve.default(100)
        self.assertEqual(curve.types, [LINEAR])
        curve = Curve([(0.0, 1.0), (40.0, 1.5), (100.0, 1.0)], 100)
        self.assertEqual(curve.types, [LINEAR, LINEAR])

    def test_endpoints_are_untouched_by_type(self):
        for kind in CURVE_TYPES:
            curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[kind])
            self.assertAlmostEqual(curve.line_speed_at(0.0), 1.0)
            self.assertAlmostEqual(curve.line_speed_at(100.0), 3.0)

    def test_ease_shapes(self):
        """v0=1.0, v1=3.0：线性 1.5 / 平滑 1.3125 / 快速 1.875 / 慢速 1.125（t=0.25）。"""
        expected = {
            LINEAR: (1.5, 2.0, 2.5),
            SMOOTH: (1.3125, 2.0, 2.6875),
            FAST: (1.875, 2.5, 2.875),
            SLOW: (1.125, 1.5, 2.125),
        }
        for kind, (q1, mid, q3) in expected.items():
            curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[kind])
            self.assertAlmostEqual(curve.line_speed_at(25.0), q1, places=6, msg=kind)
            self.assertAlmostEqual(curve.line_speed_at(50.0), mid, places=6, msg=kind)
            self.assertAlmostEqual(curve.line_speed_at(75.0), q3, places=6, msg=kind)

    def test_segments_are_independent(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.0), (100.0, 3.0)], 100, types=[SMOOTH, LINEAR])
        self.assertAlmostEqual(curve.line_speed_at(25.0), 1.0)  # 平滑段两端同值 → 恒定
        self.assertAlmostEqual(curve.line_speed_at(75.0), 2.0)  # 线性段不受影响

    def test_junk_types_fall_back_to_linear(self):
        curve = Curve([(0.0, 1.0), (50.0, 2.0), (100.0, 1.0)], 100,
                      types=["nonsense", None])
        self.assertEqual(curve.types, [LINEAR, LINEAR])

    def test_types_length_is_normalized_to_segments(self):
        curve = Curve([(0.0, 1.0), (50.0, 2.0), (100.0, 1.0)], 100, types=[SMOOTH])
        self.assertEqual(curve.types, [SMOOTH, LINEAR])
        curve = Curve([(0.0, 1.0), (100.0, 2.0)], 100, types=[SMOOTH, FAST, SLOW])
        self.assertEqual(curve.types, [SMOOTH])

    def test_hard_cut_pair_ignores_type(self):
        curve = Curve([(0.0, 1.0), (50.0, 0.5), (50.0, 1.8), (100.0, 1.8)], 100,
                      types=[SMOOTH, SMOOTH, SMOOTH])
        self.assertAlmostEqual(curve.line_speed_at(50.0), 0.5)

    def test_insert_splits_and_both_halves_inherit(self):
        curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[SMOOTH])
        curve.insert_point(50.0)
        self.assertEqual(curve.types, [SMOOTH, SMOOTH])
        # 插点取的初值是缓动曲线上的值（线不跳）
        self.assertAlmostEqual(curve.points[1][1], 2.0)

    def test_insert_into_linear_segment_keeps_linear(self):
        curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100)
        curve.insert_point(50.0)
        self.assertEqual(curve.types, [LINEAR, LINEAR])
        self.assertAlmostEqual(curve.points[1][1], 2.0)

    def test_delete_merges_taking_left_type(self):
        curve = Curve([(0.0, 1.0), (40.0, 1.5), (80.0, 2.0), (100.0, 1.0)], 100,
                      types=[SMOOTH, FAST, SLOW])
        curve.delete_point(1)
        self.assertEqual(curve.types, [SMOOTH, SLOW])
        curve.delete_point(2)
        self.assertEqual(curve.types, [SMOOTH])

    def test_swallow_drops_the_last_type(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (100.0, 2.0)], 100, types=[FAST, SLOW])
        curve.carry_tail(40.0)  # 倒数第二点(50)越过末端 → 吞掉末点
        self.assertEqual(curve.frames(), [0.0, 40.0])
        self.assertEqual(curve.types, [FAST])

    def test_types_round_trip_through_flat_payload(self):
        source = Curve([(0.0, 1.0), (40.0, 2.0), (100.0, 0.5)], 100,
                       types=[SMOOTH, SLOW])
        restored = Curve.from_flat(source.to_flat(), 100, types=source.types_to_flat())
        self.assertEqual(restored.types, [SMOOTH, SLOW])
        self.assertEqual(restored, source)

    def test_legacy_payload_without_types_reads_all_linear(self):
        curve = Curve.from_flat([0.0, 1.0, 40.0, 2.0, 100.0, 0.5], 100)
        self.assertEqual(curve.types, [LINEAR, LINEAR])

    def test_junk_type_payload_reads_all_linear(self):
        for payload in (None, [], ["a"], "bogus"):
            curve = Curve.from_flat([0.0, 1.0, 100.0, 2.0], 100, types=payload)
            self.assertEqual(curve.types, [LINEAR], msg=repr(payload))
        # 整数载荷是合法编码：认识的照解，不认识的码退化为线性
        self.assertEqual(Curve.from_flat([0.0, 1.0, 100.0, 2.0], 100, types=[1]).types, [SMOOTH])
        self.assertEqual(Curve.from_flat([0.0, 1.0, 100.0, 2.0], 100, types=[99]).types, [LINEAR])

    def test_types_are_part_of_equality(self):
        a = Curve([(0.0, 1.0), (100.0, 2.0)], 100, types=[SMOOTH])
        b = Curve([(0.0, 1.0), (100.0, 2.0)], 100, types=[LINEAR])
        self.assertNotEqual(a, b)
        self.assertEqual(a.copy(), a)

    def test_is_valid_checks_types(self):
        curve = Curve([(0.0, 1.0), (50.0, 2.0), (100.0, 1.0)], 100, types=[SMOOTH, FAST])
        self.assertTrue(curve.is_valid())
        curve.types = [SMOOTH]
        self.assertFalse(curve.is_valid())

    def test_keyframes_cover_eased_segments_per_frame(self):
        """承载需要知道"哪些帧必须有关键帧"：缓动段要逐帧采样，线性段只要端点。"""
        curve = Curve([(0.0, 1.0), (10.0, 1.0), (13.0, 2.0)], 13, types=[LINEAR, SMOOTH])
        self.assertEqual(curve.keyframe_frames(), [0.0, 10.0, 11.0, 12.0, 13.0])
        linear = Curve([(0.0, 1.0), (10.0, 1.0), (13.0, 2.0)], 13)
        self.assertEqual(linear.keyframe_frames(), [0.0, 10.0, 13.0])

    def test_constant_speed_segment_needs_only_endpoints(self):
        """恒速的非线性段：`v0 == v1` 时任何缓动都退化成常数 → 两端点足够。

        [ADR-0007 决定 6] 默认段曲线类型是平滑之后，"平坦的平滑段"是新曲线的常态；
        不改这条，在平坦的新线上插一个点就会给 120 帧素材写出 ~120 个全 1.0x 的
        关键帧。**不是**过早优化——删掉它先把下面的用例弄红。
        """
        curve = Curve([(0.0, 1.0), (10.0, 1.0), (13.0, 1.0)], 13, types=[SMOOTH, FAST])
        self.assertEqual(curve.keyframe_frames(), [0.0, 10.0, 13.0])

    def test_only_the_constant_segment_is_sparse(self):
        """混合：恒速段收敛到端点，**正在变的那段照旧逐帧**（不许一刀切成端点）。"""
        curve = Curve([(0.0, 1.0), (10.0, 1.0), (13.0, 2.0)], 13, types=[SMOOTH, SMOOTH])
        self.assertEqual(curve.keyframe_frames(), [0.0, 10.0, 11.0, 12.0, 13.0])

    def test_constant_judgement_is_exact_not_a_tolerance(self):
        """判据是**精确相等**：两端差 1e-12 也算"在变"，照旧逐帧（不引入容差魔数）。"""
        curve = Curve([(0.0, 1.0), (10.0, 1.0 + 1e-12)], 10, types=[SLOW])
        self.assertEqual(curve.keyframe_frames(),
                         [float(f) for f in range(0, 11)])


    def test_fast_is_ahead_of_the_chord_and_slow_is_behind(self):
        """「快速」= 变化集中在起步那半段 → 归一化进度恒超前线性；「慢速」反之。

        这是 ADR-0006 定的判据：按两端差值归一，所以**升段、降段同时成立**，
        与凸向无关。用户真机复核时报"快速/慢速标反了"，就是拿凸向当了判据。
        """
        for v0, v1 in ((1.0, 3.0), (3.0, 1.0), (0.5, 1.2)):
            span = v1 - v0
            fast = Curve([(0.0, v0), (100.0, v1)], 100, types=[FAST])
            slow = Curve([(0.0, v0), (100.0, v1)], 100, types=[SLOW])
            for t in (0.1, 0.25, 0.5, 0.75, 0.9):
                frame = 100.0 * t
                ahead = (fast.line_speed_at(frame) - v0) / span
                behind = (slow.line_speed_at(frame) - v0) / span
                self.assertGreater(ahead, t, "%.1f→%.1f 快速段 t=%.2f 应超前线性" % (v0, v1, t))
                self.assertLess(behind, t, "%.1f→%.1f 慢速段 t=%.2f 应落后线性" % (v0, v1, t))

    def test_bulge_flips_with_direction_but_the_name_does_not(self):
        """凸向不是判据：升段的「快速」上凸、降段的「快速」下凸，是同一个形状。

        ADR-0006 记的失败模式：这种"朝向相反"看着就像两个档名标反了。
        """
        rising = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[FAST])
        falling = Curve([(0.0, 3.0), (100.0, 1.0)], 100, types=[FAST])
        self.assertGreater(rising.line_speed_at(50.0), 2.0, "升段快速：中点高于弦 = 上凸")
        self.assertLess(falling.line_speed_at(50.0), 2.0, "降段快速：中点低于弦 = 下凸")
        # 慢速是镜像
        rising_slow = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[SLOW])
        falling_slow = Curve([(0.0, 3.0), (100.0, 1.0)], 100, types=[SLOW])
        self.assertLess(rising_slow.line_speed_at(50.0), 2.0, "升段慢速：下凸")
        self.assertGreater(falling_slow.line_speed_at(50.0), 2.0, "降段慢速：上凸")

    def test_each_menu_label_maps_to_its_declared_shape(self):
        """菜单文字 → 形状 的绑定：段菜单渲染 i18n 档名（ADR-0009）。

        票 03 复查时发现的覆盖缺口——菜单那批用例只用了「平滑」，
        真把快速/慢速标反了也没有用例会红。这条钉住"用户点的那四个字"
        各自对应哪条曲线；两种语言都钉住（源文案=英文，中文走 i18n）。
        """
        import seqvel.i18n as i18n
        self.assertEqual([TYPE_LABELS[kind] for kind in CURVE_TYPES],
                         ["Linear", "Smooth", "Fast", "Slow"])
        i18n.set_language_for_test("zh")
        try:
            self.assertEqual([i18n.type_label(kind) for kind in CURVE_TYPES],
                             ["线性", "平滑", "快速", "慢速"])
        finally:
            i18n.set_language_for_test(None)
        for label, kind, bunched_at_start in (("快速", FAST, True), ("慢速", SLOW, False)):
            i18n.set_language_for_test("zh")
            try:
                self.assertEqual(i18n.type_label(kind), label)
            finally:
                i18n.set_language_for_test(None)
            curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[kind])
            progress = (curve.line_speed_at(25.0) - 1.0) / 2.0  # 归一化进度
            if bunched_at_start:
                self.assertGreater(progress, 0.25, "「%s」起步就该走掉大半变化" % label)
            else:
                self.assertLess(progress, 0.25, "「%s」起步该平缓" % label)


class TestInvariants(unittest.TestCase):
    def test_random_operations_keep_invariants(self):
        rng = random.Random(20260914)
        curve = Curve.default(120)
        for _step in range(400):
            action = rng.choice(("insert", "move", "drag", "tail", "delete"))
            if action == "insert":
                curve.insert_point(rng.uniform(-20.0, 140.0), rng.uniform(0.0, 3.0))
            elif action == "move":
                index = rng.randrange(len(curve))
                curve.move_point(index, rel_frame=rng.uniform(-40.0, 200.0), speed=rng.uniform(-1.0, 4.0))
            elif action == "drag" and len(curve) >= 2:
                index = rng.randrange(len(curve) - 1)
                drag = curve.begin_segment_drag(index, grab_frame=rng.uniform(0.0, 120.0))
                drag.apply(rng.uniform(-1.0, 4.0))
            elif action == "tail":
                drag = curve.begin_tail_drag(grab_frame=rng.uniform(0.0, 120.0))
                drag.apply(rng.uniform(-1.0, 4.0))
            else:
                curve.delete_point(rng.randrange(len(curve)))
            self.assertTrue(curve.is_valid(), "不变量被破坏：%r" % (curve,))


class TestSampling(unittest.TestCase):
    """采样：绘制与命中必须共用同一条折线（票 03 的硬约束）。"""

    def test_samples_cover_all_points_and_split_eased_segments(self):
        curve = Curve([(0.0, 1.0), (10.0, 1.0), (13.0, 2.0)], 13, types=[LINEAR, SMOOTH])
        samples, owners = curve.sampled_points(1.0)
        self.assertEqual([f for f, _s in samples], [0.0, 10.0, 11.0, 12.0, 13.0])
        self.assertEqual(owners, [0, 1, 1, 1])
        self.assertEqual(len(owners), len(samples) - 1)

    def test_linear_segments_stay_two_point_straight(self):
        curve = Curve([(0.0, 1.0), (10.0, 1.0), (13.0, 2.0)], 13)
        samples, owners = curve.sampled_points(1.0)
        self.assertEqual([f for f, _s in samples], [0.0, 10.0, 13.0])
        self.assertEqual(owners, [0, 1])

    def test_samples_follow_the_eased_shape(self):
        curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[SMOOTH])
        samples, _owners = curve.sampled_points(10.0)
        self.assertAlmostEqual(dict(samples)[50.0], 2.0)
        fine = dict(curve.sampled_points(1.0)[0])
        self.assertAlmostEqual(fine[25.0], 1.3125)
        self.assertAlmostEqual(fine[75.0], 2.6875)

    def test_sample_step_controls_density(self):
        curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[SMOOTH])
        coarse = curve.sampled_points(50.0)[0]
        fine = curve.sampled_points(10.0)[0]
        self.assertTrue(len(fine) > len(coarse))
        self.assertEqual(coarse[0], (0.0, 1.0))
        self.assertEqual(coarse[-1], (100.0, 3.0))

    def test_sampling_tolerates_hard_cut_and_junk_step(self):
        curve = Curve([(0.0, 1.0), (50.0, 0.5), (50.0, 1.8), (100.0, 1.8)], 100)
        for step in (5.0, 0.0, -3.0, None, "x"):
            samples, owners = curve.sampled_points(step)
            self.assertEqual(len(owners), len(samples) - 1)
            self.assertEqual(samples[0][0], 0.0)
            self.assertEqual(samples[-1][0], 100.0)

    def test_default_segment_type_is_linear_in_sampling(self):
        curve = Curve.default(60)
        samples, owners = curve.sampled_points(1.0)
        self.assertEqual(len(samples), 2)
        self.assertEqual(owners, [0])


class TestDefaultKind(unittest.TestCase):
    """[ADR-0007] 默认段曲线类型：纯层只收一个显式 kind，**策略不在这里**。

    "从哪里读默认"是 store 的事——`Curve` 零 bpy、也不该知道场景。
    """

    def test_default_curve_takes_the_given_kind(self):
        for kind in CURVE_TYPES:
            curve = Curve.default(100, kind)
            self.assertEqual(curve.types, [kind], msg=kind)
            self.assertEqual(curve.points, [(0.0, 1.0), (100.0, 1.0)], msg=kind)
            self.assertTrue(curve.is_valid(), msg=kind)

    def test_default_curve_is_linear_without_a_kind(self):
        """不传 kind = 线性：纯层不带策略，既有调用方语义一字不改。"""
        self.assertEqual(Curve.default(100).types, [LINEAR])

    def test_default_curve_normalizes_junk_kind(self):
        self.assertEqual(Curve.default(100, "bogus").types, [LINEAR])
        self.assertEqual(Curve.default(100, None).types, [LINEAR])
        self.assertEqual(Curve.default(100, 1).types, [SMOOTH], "短码 1 = 平滑")

    def test_corrupt_payload_repair_takes_the_given_kind(self):
        """载荷不可用的修复路径也算"诞生" → 取默认档（ADR-0007 决定 1）。

        那条曲线两端都是 1.0x，形状上无从观察；这里钉的是"kind 被透传下去"。
        """
        curve = Curve.from_flat([1.0], 100, kind=SMOOTH)
        self.assertEqual(curve.types, [SMOOTH])
        self.assertEqual(curve.points, [(0.0, 1.0), (100.0, 1.0)])

    def test_insert_point_never_takes_a_kind(self):
        """拆段继承原段类型：`insert_point` 不该有 kind 参数（默认不介入拆段）。

        这条钉的是一个**签名**——它是"默认只管新生、不管拆段"这条决定的唯一机械
        保证：一旦有人给插点加上 `kind=SMOOTH`，已建好的线会在双击插点时当场变形。
        """
        self.assertNotIn("kind", inspect.signature(Curve.insert_point).parameters)
        curve = Curve([(0.0, 1.0), (100.0, 2.0)], 100, types=[LINEAR])
        curve.insert_point(50.0)
        self.assertEqual(curve.types, [LINEAR, LINEAR])


class TestSliceBetween(unittest.TestCase):
    """[ADR-0008 纯划分] ``Curve.slice_between``：切出某段相对区间并重定基。

    判据是**语义**的，不是数值巧合：两半的并集 = 切前逐帧、接缝两侧速度相等、
    切点落在段内部时按**该段类型**插值（而不是"把末点拖到切点、速度不变"的压缩——
    那是裁剪的「末端跟随」）。
    """

    def test_cutting_inside_a_segment_interpolates_the_seam(self):
        curve = Curve([(0.0, 1.0), (100.0, 3.0)], 100, types=[LINEAR])
        left = curve.slice_between(0.0, 50.0)
        right = curve.slice_between(50.0, 100.0)
        self.assertEqual(left.points, [(0.0, 1.0), (50.0, 2.0)])
        self.assertEqual(right.points, [(0.0, 2.0), (50.0, 3.0)])
        self.assertEqual(left.types, [LINEAR])
        self.assertEqual(right.types, [LINEAR])

    def test_union_of_the_halves_equals_the_curve_before_the_cut(self):
        """**切断不改变内容**：两半按绝对帧对齐后逐帧相同（含缓动段）。"""
        curve = Curve([(0.0, 0.5), (60.0, 2.5), (120.0, 1.0)], 120,
                      types=[SMOOTH, FAST])
        for seam in (10.0, 25.0, 60.0, 90.0, 119.0):
            left = curve.slice_between(0.0, seam)
            right = curve.slice_between(seam, 120.0)
            for frame in range(0, 121):
                part = left if frame <= seam else right
                rel = float(frame) if frame <= seam else float(frame) - seam
                self.assertAlmostEqual(part.line_speed_at(rel),
                                       curve.line_speed_at(float(frame)), places=9,
                                       msg="第 %d 帧（切点 %.1f）" % (frame, seam))

    def test_seam_speeds_are_equal_for_every_segment_type(self):
        """接缝两侧速度恒相等——"切断不留台阶"的机械判据（ADR-0008 决定 1）。"""
        for kind in CURVE_TYPES:
            curve = Curve([(0.0, 1.0), (80.0, 2.0)], 80, types=[kind])
            left = curve.slice_between(0.0, 33.0)
            right = curve.slice_between(33.0, 80.0)
            self.assertAlmostEqual(left.line_speed_at(33.0), right.line_speed_at(0.0),
                                   places=9, msg=kind)

    def test_point_exactly_on_the_seam_is_shared_not_duplicated(self):
        curve = Curve([(0.0, 1.0), (50.0, 2.0), (100.0, 1.0)], 100,
                      types=[LINEAR, LINEAR])
        self.assertEqual(curve.slice_between(0.0, 50.0).points,
                         [(0.0, 1.0), (50.0, 2.0)])
        self.assertEqual(curve.slice_between(50.0, 100.0).points,
                         [(0.0, 2.0), (50.0, 1.0)])

    def test_straddled_eased_segment_is_resampled_whole_segments_keep_their_type(self):
        """被劈开的缓动段逐帧展开（小段线性）；整段落在切区里的段连类型一起保留。"""
        curve = Curve([(0.0, 1.0), (50.0, 2.0), (100.0, 1.5)], 100,
                      types=[SMOOTH, FAST])
        right = curve.slice_between(40.0, 100.0)
        self.assertEqual(right.points[0][0], 0.0)
        self.assertAlmostEqual(right.points[-1][0], 60.0, places=9)
        self.assertEqual(right.points[-1][1], 1.5)
        self.assertEqual(right.segment_type(0), LINEAR)
        self.assertEqual(right.segment_type(len(right.types) - 1), FAST)
        for frame in range(40, 101):  # 并集 = 切前逐帧
            self.assertAlmostEqual(right.line_speed_at(float(frame - 40)),
                                   curve.line_speed_at(float(frame)), places=9,
                                   msg="第 %d 帧" % frame)
        self.assertTrue(right.is_valid())

    def test_half_of_an_eased_segment_is_never_reshaped(self):
        """[ADR-0008 决定 1/2] 被劈开的平滑段**不许**把该档重新铺满缩短后的区间。

        两个数都摆在明面上：原曲线在第 1 帧是 0.5016…，"重新铺满"会变成 0.5041…
        ——铺满会把"画出来的线"与"真正播出来的速度"拆开（承载侧是按绝对帧逐帧
        写 key 的），所以这里宁可逐帧展开。
        """
        curve = Curve([(0.0, 0.5), (60.0, 2.5), (120.0, 1.0)], 120,
                      types=[SMOOTH, FAST])
        left = curve.slice_between(0.0, 10.0)
        self.assertAlmostEqual(left.line_speed_at(1.0), curve.line_speed_at(1.0),
                               places=9)
        self.assertNotAlmostEqual(left.line_speed_at(1.0), 0.5041481481481481,
                                  places=6, msg="这是「把平滑档铺满 0..10」的值")

    def test_curve_entirely_before_the_seam_becomes_a_flat_line(self):
        """曲线整体在切点之前：后半退化为该区间上的外推水平线（最近端点速度）。

        并集也要对得上——这一档的"两半"是"曲线还在的那半 + 只有外推线的那半"，
        跟其他两种切点位置一样要逐帧拼回切前曲线（规格「验收判据」三种位置之一）。
        """
        curve = Curve([(0.0, 1.0), (30.0, 2.4)], 100, types=[LINEAR])
        left = curve.slice_between(0.0, 49.0)
        right = curve.slice_between(49.0, 100.0)
        self.assertEqual(right.points, [(0.0, 2.4), (51.0, 2.4)])
        self.assertEqual(right.types, [LINEAR])
        for frame in range(0, 101):
            part = left if frame < 49.0 else right
            rel = float(frame) if frame < 49.0 else float(frame) - 49.0
            self.assertAlmostEqual(part.line_speed_at(rel),
                                   curve.line_speed_at(float(frame)), places=6,
                                   msg="第 %d 帧" % frame)

    def test_slices_always_satisfy_the_model_invariants(self):
        curve = Curve([(0.0, 0.5), (37.0, 3.0), (120.0, 0.2)], 120,
                      types=[FAST, SLOW])
        for low, high in ((0.0, 37.0), (37.0, 120.0), (10.0, 100.0), (0.0, 1.0)):
            part = curve.slice_between(low, high)
            self.assertTrue(part.is_valid(), "切片 %r..%r" % (low, high))
            self.assertEqual(part.points[0][0], 0.0)
            self.assertAlmostEqual(part.duration, high - low, places=9)


if __name__ == "__main__":
    unittest.main()
