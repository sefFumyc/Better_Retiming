# -*- coding: utf-8 -*-
"""拖动临时读数（ADR-0005 修订 R10）的判定与状态通道。

读数的**语义**只有一句话：勾选「显示速度百分比」= 全部点常显；未勾选时平日没有
数字，只有拖动中**临时**显示"本次正在改变的那些点"。这个模块把两件事钉死：

- `overlay.visible_hud_indices`：本次绘制该显示哪些点的百分比（纯决策，好测）；
- `status` 的拖动目标通道：交互把"这次拖动会改变哪些点"放进去、绘制读出来、
  收摊清掉。
"""

import unittest

from seqvel import status, store
from seqvel.curve import SMOOTH, Curve

from support import (HAVE_BPY, FakeView2D, make_editor_context, make_strip,
                     reset_scene)

if HAVE_BPY:
    import bpy

    from seqvel import carrier, i18n, overlay


def setUpModule():
    # 本文件断言中文诊断文案（ADR-0009：运行层默认 en，中文经 i18n 提供）
    if HAVE_BPY:
        i18n.set_language_for_test("zh")


def tearDownModule():
    if HAVE_BPY:
        i18n.set_language_for_test(None)


class TestDragTargetChannel(unittest.TestCase):
    """状态通道本身（零 bpy）：设、读、清；"没有目标"必须就是 None。"""

    def setUp(self):
        status.clear_drag_target()

    def tearDown(self):
        status.clear_drag_target()

    def test_starts_empty(self):
        self.assertIsNone(status.drag_target())

    def test_set_and_read(self):
        status.set_drag_target(("scene", "strip"), (1, 2))
        self.assertEqual(status.drag_target(), (("scene", "strip"), (1, 2)))

    def test_indices_are_frozen_to_a_tuple(self):
        """列表进来也必须变成元组：绘制端每帧读它，不该能被就地改。"""
        status.set_drag_target(("s", "t"), [3])
        _key, indices = status.drag_target()
        self.assertEqual(indices, (3,))
        self.assertIsInstance(indices, tuple)

    def test_emptiness_is_none_not_an_empty_tuple(self):
        """空集合 = 没有点要显形，等价于"没在拖"——统一收敛成 None。"""
        status.set_drag_target(("s", "t"), ())
        self.assertIsNone(status.drag_target())
        status.set_drag_target(("s", "t"), (1,))
        status.clear_drag_target()
        self.assertIsNone(status.drag_target())

    def test_junk_inputs_never_raise(self):
        status.set_drag_target(None, None)
        self.assertIsNone(status.drag_target())
        status.set_drag_target("nonsense", None)
        self.assertIsNone(status.drag_target())


@unittest.skipUnless(HAVE_BPY, "需要 bpy（overlay 顶层 import gpu/blf）")
class TestVisibleHudIndices(unittest.TestCase):
    """决策表：勾选 = 全部；未勾选 = 只显示**本次被改变**的那些点。"""

    def setUp(self):
        self.strip = make_strip(length=120)
        self.curve = Curve([(0.0, 1.0), (40.0, 1.4), (120.0, 1.0)], 120.0,
                           types=[SMOOTH, SMOOTH])
        self.key = store.strip_key(self.strip)

    def indices(self, show_all, target):
        return overlay.visible_hud_indices(self.strip, self.curve, show_all, target)

    def test_checked_shows_every_point(self):
        self.assertEqual(self.indices(True, None), {0, 1, 2})
        self.assertEqual(self.indices(True, (self.key, (1,))), {0, 1, 2},
                         "勾选态下临时机制被完全覆盖，不做第三态")

    def test_unchecked_shows_nothing_when_idle(self):
        self.assertEqual(self.indices(False, None), set())

    def test_unchecked_shows_only_the_dragged_point(self):
        self.assertEqual(self.indices(False, (self.key, (1,))), {1})

    def test_unchecked_shows_both_ends_of_a_dragged_segment(self):
        self.assertEqual(self.indices(False, (self.key, (0, 1))), {0, 1})
        self.assertEqual(self.indices(False, (self.key, (1, 2))), {1, 2})

    def test_another_strip_being_dragged_shows_nothing(self):
        other = make_strip(length=60, name="other")
        self.assertEqual(self.indices(False, (("scene", other.name), (0, 1))), set())
        self.assertEqual(self.indices(False, (("another_scene", self.strip.name), (0,))),
                         set())

    def test_out_of_range_indices_are_dropped(self):
        self.assertEqual(self.indices(False, (self.key, (2, 7))), {2})
        self.assertEqual(self.indices(False, (self.key, (99,))), set())


@unittest.skipUnless(HAVE_BPY, "需要 bpy（overlay 顶层 import gpu/blf）")
class TestDrawReason(unittest.TestCase):
    """绘制诊断："本次临时显形了几个点"必须写进面板常驻那行（不静默）。

    全屏文字 HUD 在真机上读不了像素——不记账，"数字其实没冒出来"无从证伪。
    """

    def test_reason_mentions_transient_readout(self):
        self.assertEqual(overlay._draw_reason(2, 0), "ok(2)")
        self.assertEqual(overlay._draw_reason(2, 2), "ok(2) · 临时 2点")
        self.assertEqual(overlay._draw_reason(1, 1), "ok(1) · 临时 1点")

    def test_nothing_drawn_says_so(self):
        self.assertEqual(overlay._draw_reason(0, 0), "无待绘制条带")


@unittest.skipUnless(HAVE_BPY, "需要 bpy（overlay 顶层 import gpu/blf）")
class TestDrawAllBooksTheTransientReadout(unittest.TestCase):
    """`_draw_all` 的集成：决策 → 交给 `draw_strip` 的 HUD 集合 → 绘制诊断。

    两侧各自单测过（`visible_hud_indices` 是决策、`_draw_reason` 是文案），但把
    它们接起来的那个循环没人测过——而"未勾选时拖动能看见数字"正是靠它落地：
    判断读场景设置与拖动状态、把集合传下去、把"临时显形了几个点"算进诊断。

    后台没有 GPU 着色器与真实绘制原语，替掉 `_get_shader` 与 `draw_strip`；
    条带枚举、HUD 取舍、记账、文案全是真的。
    """

    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        status.clear_drag_target()
        import seqvel

        self.seqvel = seqvel
        self.seqvel.register()          # 场景要挂上 seqvel 设置组才有 show_hud
        self.strip = make_strip(length=120)
        self.strip.select = True
        carrier.STORE.get_or_create(self.strip)   # 建线 → 常开显示的可见条带
        self.key = store.strip_key(self.strip)
        self.context, _regions = make_editor_context(bpy.context.scene,
                                                     FakeView2D())
        self.settings = bpy.context.scene.seqvel
        self.saved_shader = overlay._get_shader
        self.saved_draw = overlay.draw_strip
        overlay._get_shader = lambda: object()
        self.seen = []
        overlay.draw_strip = lambda *a, **k: (
            self.seen.append(k.get("hud_indices")), True)[1]

    def tearDown(self):
        overlay._get_shader = self.saved_shader
        overlay.draw_strip = self.saved_draw
        status.clear_drag_target()
        try:
            self.seqvel.unregister()
        except Exception:
            pass
        store.STORE.invalidate()

    def draw(self):
        self.seen[:] = []
        return overlay._draw_all(self.context)

    def test_unchecked_with_a_drag_target_shows_exactly_those_points(self):
        self.settings.show_hud = "OFF"
        status.set_drag_target(self.key, (1,))
        self.assertEqual(self.draw(), "ok(1) · 临时 1点")
        self.assertEqual(self.seen, [{1}], "集合必须原样交给 draw_strip")

    def test_a_dragged_segment_shows_both_ends(self):
        self.settings.show_hud = "OFF"
        status.set_drag_target(self.key, (0, 1))
        self.assertEqual(self.draw(), "ok(1) · 临时 2点")
        self.assertEqual(self.seen, [{0, 1}])

    def test_unchecked_and_idle_draws_no_numbers_and_says_nothing_extra(self):
        self.settings.show_hud = "OFF"
        self.assertEqual(self.draw(), "ok(1)")
        self.assertEqual(self.seen, [set()], "平日就是空集合")

    def test_another_strip_being_dragged_does_not_light_this_one_up(self):
        self.settings.show_hud = "OFF"
        status.set_drag_target(("别的场景", "别的条带"), (0, 1))
        self.assertEqual(self.draw(), "ok(1)")
        self.assertEqual(self.seen, [set()])

    def test_checked_shows_every_point_and_drops_the_transient_suffix(self):
        """勾选态语义一字不改：全部点常显，临时后缀不出现（不做第三态）。"""
        self.settings.show_hud = "ON"
        status.set_drag_target(self.key, (1,))
        self.assertEqual(self.draw(), "ok(1)")
        self.assertEqual(self.seen, [{0, 1}], "两点默认曲线 → 全部点")

    def test_release_stops_the_report(self):
        """松手（目标清掉）之后，诊断行必须立刻不再提"临时"。"""
        self.settings.show_hud = "OFF"
        status.set_drag_target(self.key, (0,))
        self.assertIn("临时", self.draw())
        status.clear_drag_target()
        self.assertEqual(self.draw(), "ok(1)")


if __name__ == "__main__":
    unittest.main()
