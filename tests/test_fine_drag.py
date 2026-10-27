# -*- coding: utf-8 -*-
"""Ctrl 微调拖动（ADR-0005 修订 R9）的模型层单测（纯 Python，零 bpy）。

语义判据（R9 定案，见 docs/adr/0005 修订 R9）：
- 减半只作用在**增量**上：变化量 = 0.5 × (光标速度 − 锚点速度)，无步进吸附；
- 点拖微调是**锚定式**而非绝对吸附：开启瞬间零跳变，帧冻结（仅 Y）；
- 段拖 / 尾段拖本就只改速度，微调对它们只是增量乘 0.5；
- 实时开关：每次翻转重锚（快照 = 当前值、锚 = 当前光标速度），零跳变；
- 钳制仍走 ``[SPEED_MIN, 显示范围上限]``（会话显式传 cap），微调不引入新取整；
- [R11] 锚定输入**去饱和**：光标越过标尺边界后增量继续增长，点停在值域边界
  （而不是 0.5 × 锚点到边界距离处）。
"""

import unittest

from seqvel.curve import FINE_SCALE, SPEED_MAX, SPEED_MIN, Curve, yfrac_to_speed_raw

from support import HAVE_BPY

if HAVE_BPY:
    import types

    import bpy

    from seqvel import geometry, interaction, status
    from seqvel.curve import set_active_cap
    from support import FakeView2D, make_editor_context, make_strip, reset_scene

ANCHOR = 1.0  # 锚点速度（= 微调生效瞬间的光标速度）


class TestFinePointDrag(unittest.TestCase):
    """点拖微调：锚定式 + 增量减半 + 帧冻结。"""

    def setUp(self):
        self.curve = Curve([(0.0, 1.0), (50.0, 1.5), (120.0, 1.0)], 120.0)
        self.index = 1  # 中间点 (50.0, 1.5)

    def _drag(self):
        return self.curve.begin_point_drag(self.index, ANCHOR, FINE_SCALE)

    def test_half_of_the_cursor_delta_is_applied(self):
        drag = self._drag()
        drag.apply(ANCHOR + 0.2)
        self.assertAlmostEqual(self.curve.points[self.index][1], 1.6,
                               "光标挪 0.2x，点只挪一半 0.1x")

    def test_frame_stays_frozen(self):
        """微调只沿 Y：帧一个数都不动（这是 Ctrl 对点拖的真实新增约束）。"""
        drag = self._drag()
        drag.apply(ANCHOR + 0.4)
        self.assertEqual(self.curve.points[self.index][0], 50.0)

    def test_anchored_not_absolute(self):
        """锚定式：从点的旧值叠加减半增量，不是吸附到光标速度上。"""
        drag = self._drag()
        drag.apply(ANCHOR + 0.4)
        # 绝对吸附会得到 1.4；锚定减半应得 1.5 + 0.5×0.4 = 1.7
        self.assertAlmostEqual(self.curve.points[self.index][1], 1.7)

    def test_activating_does_not_jump(self):
        """生效瞬间（光标还在锚点上）零跳变。"""
        drag = self._drag()
        drag.apply(ANCHOR)
        self.assertEqual(self.curve.points[self.index][1], 1.5)

    def test_clamp_still_applies_in_fine_mode(self):
        """微调不加步进、不加新取整，但值域钳制原样生效。

        光标速度先经既有钳制再算增量（1:1 拖动同款语义）：点 2.9x、锚 1.0x、
        光标顶到 3.0x → 增量 +1.0 → 目标 3.9x 被钳回 SPEED_MAX。
        """
        curve = Curve([(0.0, 1.0), (50.0, 2.9), (120.0, 1.0)], 120.0)
        drag = curve.begin_point_drag(1, 1.0, FINE_SCALE, cap=SPEED_MAX)
        drag.apply(3.0)
        self.assertEqual(curve.points[1][1], 3.0)  # SPEED_MAX

    def test_fine_scale_is_half(self):
        self.assertEqual(FINE_SCALE, 0.5)

    def test_out_of_range_index_is_refused(self):
        with self.assertRaises(IndexError):
            self.curve.begin_point_drag(99, ANCHOR, FINE_SCALE)


class TestFineDragInputSaturation(unittest.TestCase):
    """[R11] 锚定输入去饱和：会话吃**未钳**光标值，越界后增量继续增长。

    真实破坏（用户报的体感）：点在 12%，按住 Ctrl 往下拖，光标越过条带下沿后
    映射被钳死在 SPEED_MIN，0.5 增益下点只走到 0.5×(0.12−0.01)=0.055 处
    （≈7%）就"沼泽化"挪不动了。语义判据：点必须一路走到**值域边界**。
    """

    def _curve_at_twelve_percent(self):
        return Curve([(0.0, 0.12), (120.0, 0.12)], 120.0)

    def test_cursor_below_the_ruler_keeps_pushing_down(self):
        """光标越过下沿（未钳值 −0.10）后增量继续增长：点走到 SPEED_MIN。"""
        curve_model = self._curve_at_twelve_percent()
        drag = curve_model.begin_point_drag(1, 0.12, FINE_SCALE, cap=SPEED_MAX)
        drag.apply(-0.10)  # 0.12 − 0.5×(0.12−(−0.10)) = 0.01 = SPEED_MIN
        self.assertAlmostEqual(curve_model.points[1][1], SPEED_MIN,
                               msg="光标越界后必须继续生效到值域边界，不能卡在半路（7%）")

    def test_cursor_above_the_ruler_reaches_the_cap(self):
        """光标越过上沿同理：上行一直走到会话 cap 才停。"""
        curve_model = self._curve_at_twelve_percent()
        drag = curve_model.begin_point_drag(1, 0.12, FINE_SCALE, cap=SPEED_MAX)
        drag.apply(0.12 + 2.0 * (SPEED_MAX - 0.12))  # 未钳光标值 5.88
        self.assertEqual(curve_model.points[1][1], SPEED_MAX)

    def test_segment_reanchor_with_cursor_outside_does_not_jump(self):
        """光标在界外时重锚也零跳变，且后续增量按原始值继续算。"""
        curve_model = Curve([(0.0, 1.0), (120.0, 1.0)], 120.0)
        drag = curve_model.begin_segment_drag(0, 60.0, FINE_SCALE)
        drag.reanchor(-0.5, FINE_SCALE)  # 光标已在条带下方（未钳值 −0.5）
        self.assertEqual([p[1] for p in curve_model.points], [1.0, 1.0],
                         "界外重锚同样零跳变")
        drag.apply(-0.4)  # 再往上挪 0.1 → 0.5×0.1 = 0.05
        self.assertAlmostEqual(curve_model.points[0][1], 1.05)

    def test_one_to_one_session_still_lands_on_the_value_floor(self):
        """回归守卫：1:1 会话的可见结果不变——端点照样被值域边界钳住。"""
        curve_model = Curve([(0.0, 1.0), (120.0, 1.0)], 120.0)
        drag = curve_model.begin_segment_drag(0, 60.0)
        drag.apply(-5.0)
        self.assertAlmostEqual(curve_model.points[0][1], SPEED_MIN,
                               msg="1:1 端点照样被值域下界钳住")
        self.assertAlmostEqual(curve_model.points[1][1], SPEED_MIN)


if HAVE_BPY:

    class _DragOp(object):
        """operator 替身：绑定真 ``_apply_drag``，其余状态手工布景。"""

        _apply_drag = interaction.SEQVEL_OT_edit_speedline._apply_drag
        _retune = interaction.SEQVEL_OT_edit_speedline._retune
        _header_text = interaction.SEQVEL_OT_edit_speedline._header_text
        _mouse = interaction.SEQVEL_OT_edit_speedline._mouse
        _view2d = interaction.SEQVEL_OT_edit_speedline._view2d
        _base_frame = interaction.SEQVEL_OT_edit_speedline._base_frame
        _channel = interaction.SEQVEL_OT_edit_speedline._channel

    @unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
    class TestMidDragFineEntryKeepsPressCap(unittest.TestCase):
        """[R11 回归] 拖动中才开启微调（重锚/补建会话）必须沿用**按下那一刻**
        的显示档，不能退回数据硬顶 10.0——否则无 Ctrl 起拖、中途按 Ctrl 就能把
        点推过显示档（审查发现的真缺陷）。
        """

        def setUp(self):
            reset_scene()
            self.strip = make_strip(length=120)
            self.view2d = FakeView2D()
            self.context, self.regions = make_editor_context(
                bpy.context.scene, self.view2d, active="WINDOW")
            self.base = float(self.strip.left_handle)
            self.channel = float(self.strip.channel)
            status.set_editing("")

        def tearDown(self):
            status.set_editing("")
            set_active_cap(SPEED_MAX)

        def _event_above(self, rel_frame, raw_speed, ctrl):
            """造"光标越界"的 MOVE 事件：速度超过显示档的点画在标尺顶
            （``speed_to_yfrac`` 钳显示，标尺顶 = 素材上缘，ADR-0010），所以
            越界像素要从标尺顶像素按同一换算率继续向上外插。
            """
            top = geometry.point_xy(self.view2d, self.base, self.channel,
                                    rel_frame, SPEED_MAX)
            bottom = geometry.point_xy(self.view2d, self.base, self.channel,
                                       rel_frame, SPEED_MIN)
            up = top[1] - bottom[1]  # "更高速度"方向的像素步长（带符号）
            y = top[1] + (raw_speed - SPEED_MAX) / SPEED_MAX * up
            region = self.regions["WINDOW"]
            return types.SimpleNamespace(
                type="MOUSEMOVE",
                mouse_x=region.x + top[0],
                mouse_y=region.y + y,
                ctrl=ctrl,
            )

        def _op(self, curve_model):
            op = _DragOp()
            op.strip = self.strip
            op.curve = curve_model
            op.mode = "POINT"
            op.index = 1
            op.fine = False
            op.drag = None
            op._cap = SPEED_MAX  # 按下那一刻的档（_begin_drag 冻结）
            return op

        def test_mid_drag_fine_entry_clamps_at_press_cap(self):
            """无 Ctrl 起拖、中途按 Ctrl：会话补建按显示档收口，点不过 3.0。"""
            curve_model = Curve([(0.0, 2.9), (120.0, 2.9)], 120.0)
            op = self._op(curve_model)
            # 第一拍：按住 Ctrl 进入微调（重锚补建会话，锚 = 未钳光标 3.5）
            op._apply_drag(self.context,
                           self._event_above(60.0, 3.5, ctrl=True))
            # 第二拍：光标继续上行到未钳值 5.5——点必须被按下档钳在 3.0
            op._apply_drag(self.context,
                           self._event_above(60.0, 5.5, ctrl=True))
            self.assertAlmostEqual(curve_model.points[1][1], SPEED_MAX,
                                   msg="中途进微调也要按按下那一刻的显示档收口")


class TestFineTuneReanchor(unittest.TestCase):
    """实时开关（R9）：翻转瞬间重锚，零跳变；回 1:1 恢复满速。"""

    def setUp(self):
        self.curve = Curve([(0.0, 1.0), (120.0, 1.0)], 120.0)

    def test_segment_reanchor_switches_without_jump(self):
        drag = self.curve.begin_segment_drag(0, 60.0)  # 锚 = 线上速度 1.0
        drag.apply(1.5)
        self.assertEqual([p[1] for p in self.curve.points], [1.5, 1.5])

        drag.reanchor(1.5, FINE_SCALE)  # 拖动中按住 Ctrl：重锚到当前状态
        self.assertEqual([p[1] for p in self.curve.points], [1.5, 1.5],
                         "重锚瞬间零跳变")
        drag.apply(1.7)
        self.assertAlmostEqual(self.curve.points[0][1], 1.6,
                               "翻转后光标再挪 0.2x，端点只挪一半")

        drag.reanchor(1.7, 1.0)  # 松开 Ctrl：回到 1:1
        drag.apply(1.9)
        self.assertAlmostEqual(self.curve.points[0][1], 1.8, "回 1:1 后恢复满速")

    def test_tail_reanchor_scales_the_last_point_only(self):
        curve = Curve([(0.0, 1.0), (40.0, 1.2), (120.0, 1.0)], 120.0)
        drag = curve.begin_tail_drag(80.0)  # 抓尾段延长线（末点右侧）
        drag.reanchor(1.0, FINE_SCALE)
        drag.apply(1.4)
        self.assertAlmostEqual(curve.points[-1][1], 1.2, msg="末点只挪一半")
        self.assertEqual([p[1] for p in curve.points], [1.0, 1.2, 1.2],
                         "尾段拖只动末点")

    def test_begin_with_fine_scale_halves_from_the_start(self):
        """Ctrl **起拖**（不是中途翻转）也要直接进减半档（R9：按下即微调）。"""
        curve = Curve([(0.0, 1.0), (120.0, 1.0)], 120.0)
        drag = curve.begin_segment_drag(0, 60.0, FINE_SCALE)
        drag.apply(1.4)  # 锚 = 抓取处线上速度 1.0
        self.assertAlmostEqual(curve.points[0][1], 1.2, msg="起拖即减半")
        tail_curve = Curve([(0.0, 1.0), (40.0, 1.0), (120.0, 1.0)], 120.0)
        tail_drag = tail_curve.begin_tail_drag(80.0, FINE_SCALE)
        tail_drag.apply(1.4)
        self.assertAlmostEqual(tail_curve.points[-1][1], 1.2, msg="尾段同理")

    def test_reanchor_without_scale_keeps_current_scale(self):
        drag = self.curve.begin_segment_drag(0, 60.0)
        drag.reanchor(1.2, FINE_SCALE)
        drag.reanchor(1.2)
        self.assertEqual(drag.scale, FINE_SCALE, "不传 scale 就是只重锚不换档")

    def test_default_scale_is_one(self):
        """既有路径必须原样 1:1：默认 scale=1，apply 语义不变。"""
        drag = self.curve.begin_segment_drag(0, 60.0)
        self.assertEqual(drag.scale, 1.0)
        drag.apply(1.5)
        self.assertEqual([p[1] for p in self.curve.points], [1.5, 1.5])

    def test_begin_point_drag_with_full_scale_stays_one_to_one(self):
        """scale 是参数不是魔法：传 1.0 的点拖锚定会话就是普通锚定拖动。"""
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (120.0, 1.0)], 120.0)
        drag = curve.begin_point_drag(1, 1.0, 1.0)
        drag.apply(1.9)
        self.assertAlmostEqual(curve.points[1][1], 2.4)
        self.assertEqual(curve.points[1][0], 50.0, "锚定会话的帧都不动")


if __name__ == "__main__":
    unittest.main()
