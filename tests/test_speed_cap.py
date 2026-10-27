# -*- coding: utf-8 -*-
"""显示范围上限（CONTEXT.md「显示范围上限」，v1.5.0）的模型层单测（纯 Python）。

语义判据（grilling 定稿）：
- 上限是**显示/拖动**概念：映射函数吃当前 ACTIVE_CAP，可实时改；
- **数据不动**：构造 / 读回 / clamp_speed 缺省钳到数据硬顶 SPEED_CEIL（10.0），
  不被当前显示上限吃掉——上限调小后超限点原样保留；
- 拖动钳制走**会话 cap**（按下那一刻的档），拖动中改设置不追改会话；
- 出厂档 3.0：1.0x 落在标尺 1/3 处（通道高的 9%~95%，跨度 0.86；Blender 内缩 5%
  + 插件底部留白 4%，ADR-0010 修订 R1）。
"""

import unittest

from seqvel import curve
from seqvel.curve import (
    SPEED_CEIL,
    SPEED_MAX,
    SPEED_MIN,
    Curve,
    set_active_cap,
    speed_to_yfrac,
    yfrac_to_speed,
    yfrac_to_speed_raw,
)


def _restore_cap(method):
    """装饰器：跑完恢复出厂档（全局 ACTIVE_CAP 不许泄漏到别的用例）。"""
    def wrapper(self):
        try:
            method(self)
        finally:
            set_active_cap(SPEED_MAX)
    return wrapper


class TestActiveCapState(unittest.TestCase):
    def test_default_cap_is_factory_three_x(self):
        self.assertEqual(curve.active_cap(), SPEED_MAX)

    @_restore_cap
    def test_cap_switches_live(self):
        self.assertEqual(set_active_cap(10.0), 10.0)
        self.assertEqual(curve.active_cap(), 10.0)
        set_active_cap(SPEED_MAX)
        self.assertEqual(curve.active_cap(), SPEED_MAX)

    def test_cap_is_clamped_into_legal_range(self):
        self.assertEqual(set_active_cap(0.5), SPEED_MAX)   # 低于下档 → 回出厂
        self.assertEqual(set_active_cap(99.0), SPEED_CEIL)  # 超过硬顶 → 顶住
        self.assertEqual(set_active_cap("junk"), SPEED_MAX)
        set_active_cap(SPEED_MAX)


class TestDisplayMappingFollowsCap(unittest.TestCase):
    @_restore_cap
    def test_yfrac_mapping_uses_active_cap(self):
        set_active_cap(10.0)
        self.assertAlmostEqual(speed_to_yfrac(10.0), 0.95)  # 上限贴素材上缘
        self.assertAlmostEqual(speed_to_yfrac(1.0), 0.09 + 0.86 * 0.1)  # 标尺 1/10 处
        self.assertAlmostEqual(yfrac_to_speed(1.0), 10.0)  # 光标越过素材上缘仍钳到 10x
        self.assertEqual(yfrac_to_speed(-0.5), SPEED_MIN)

    @_restore_cap
    def test_above_cap_clamps_display_not_data(self):
        # 超限点：显示钳到素材上缘（比例 0.95），数据原样
        set_active_cap(SPEED_MAX)
        self.assertAlmostEqual(speed_to_yfrac(5.0), 0.95)
        curve_model = Curve([(0.0, 5.0), (100.0, 5.0)], 100)
        self.assertEqual(curve_model.speeds(), [5.0, 5.0])
        self.assertTrue(curve_model.is_valid())  # ≤ SPEED_CEIL 即合法

    def test_default_cap_keeps_one_x_at_one_third_of_the_ruler(self):
        set_active_cap(SPEED_MAX)
        self.assertAlmostEqual(speed_to_yfrac(1.0), 0.09 + 0.86 / 3.0)


class TestRawCursorMapping(unittest.TestCase):
    """[R11] 锚定会话的原始光标映射：线性外插、不钳标尺。

    判据：条带内与钳制版逐值一致（手感不变）；越界后按同一换算率继续
    外插（下沿以下为负、上沿以上超 cap），供会话增量继续增长。
    """

    @_restore_cap
    def test_raw_mapping_agrees_inside_the_ruler(self):
        set_active_cap(SPEED_MAX)
        # 0.52 恰是标尺（0.09~0.95）的中点 → 1.5x
        self.assertAlmostEqual(yfrac_to_speed_raw(0.52, SPEED_MAX), 1.5)
        self.assertAlmostEqual(yfrac_to_speed_raw(0.52, SPEED_MAX),
                               yfrac_to_speed(0.52, SPEED_MAX))

    @_restore_cap
    def test_raw_mapping_extends_beyond_the_ruler(self):
        set_active_cap(SPEED_MAX)
        # 界外线性外插，同一换算率 cap/0.86（标尺跨 0.09~0.95，ADR-0010 修订 R1）：
        # (-0.1−0.09)/0.86×3 ≈ −0.663；(1.2−0.09)/0.86×3 ≈ 3.872
        self.assertAlmostEqual(yfrac_to_speed_raw(-0.1, SPEED_MAX),
                               (-0.1 - 0.09) / 0.86 * SPEED_MAX)
        self.assertAlmostEqual(yfrac_to_speed_raw(1.2, SPEED_MAX),
                               (1.2 - 0.09) / 0.86 * SPEED_MAX)

    @_restore_cap
    def test_raw_mapping_follows_active_cap(self):
        set_active_cap(10.0)
        # (0.2−0.09)/0.86×10 ≈ 1.279；(−0.2−0.09)/0.86×10 ≈ −3.372
        self.assertAlmostEqual(yfrac_to_speed_raw(0.2), (0.2 - 0.09) / 0.86 * 10.0)
        self.assertAlmostEqual(yfrac_to_speed_raw(-0.2), (-0.2 - 0.09) / 0.86 * 10.0)


class TestDataUnaffectedByCap(unittest.TestCase):
    @_restore_cap
    def test_load_path_ignores_display_cap(self):
        # 上限 3.0 下加载 1000% 数据：数据不动（"下次拖动才钳"）
        set_active_cap(SPEED_MAX)
        curve_model = Curve([(0.0, 8.0), (100.0, 2.0)], 100)
        self.assertEqual(curve_model.speeds(), [8.0, 2.0])

    @_restore_cap
    def test_drag_clamps_to_session_cap(self):
        # 会话 cap 在按下那一刻定型；拖动中改设置不追改
        set_active_cap(10.0)
        curve_model = Curve([(0.0, 1.0), (100.0, 1.0)], 100)
        drag = curve_model.begin_segment_drag(0, grab_frame=50.0)
        set_active_cap(SPEED_MAX)  # 会话建立后改设置
        drag.apply(5.0)
        self.assertEqual(curve_model.speeds(), [5.0, 5.0])  # 仍按 10.0 档

    @_restore_cap
    def test_drag_at_low_cap_clamps_cursor(self):
        set_active_cap(SPEED_MAX)
        curve_model = Curve([(0.0, 1.0), (100.0, 1.0)], 100)
        drag = curve_model.begin_segment_drag(0, grab_frame=50.0, cap=SPEED_MAX)
        drag.apply(9.0)  # 光标 9x，但当前档 3.0 → 钳到 3.0
        self.assertEqual(curve_model.speeds(), [SPEED_MAX, SPEED_MAX])
