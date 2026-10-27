# -*- coding: utf-8 -*-
"""悬停光标（ADR-0005 修订 R3，v1.2.2 重写）：MOUSEMOVE 键位触发 → HAND_POINT。

为什么不是定时器轮询：轮询需要读鼠标位置，而 **bpy.types.Window 没有
mouse_x/mouse_y**（5.2.1 RNA 实测——v1.2.1 的定时器轮询因此从未生效）。
Blender 没有悬停事件、也没有可轮询的鼠标位置，唯一可靠的通道是键位：
把悬停 operator 绑到 Sequencer 的 ``MOUSEMOVE`` 上，每次移动唤醒一次
命中检测，然后**一律 PASS_THROUGH**——它是旁观者，不许吃任何事件。

光标是窗口级状态，替身窗口记录 ``cursor_set`` 调用序列来断言（后台模式
没有真窗口，改不了也看不了真光标）。
"""

import types
import unittest

from seqvel import geometry

from support import (HAVE_BPY, FakeView2D, make_editor_context,
                     make_strip, reset_scene)

if HAVE_BPY:
    import bpy

    from seqvel import carrier, hover, status, store


def _fake_window():
    """替身窗口：记录 cursor_set 调用（没有 mouse_x——真窗口也没有）。"""
    calls = []

    def _record(name):
        calls.append(name)

    return types.SimpleNamespace(cursor_set=_record), calls


if HAVE_BPY:

    class _Op(object):
        """operator 替身：bpy operator 类不能直接实例化，绑定真方法即可。"""

        invoke = hover.SEQVEL_OT_hover_cursor.invoke
        _start = hover.SEQVEL_OT_hover_cursor._start

    class TestHoverCursor(unittest.TestCase):
        def setUp(self):
            reset_scene()
            store.STORE.invalidate()
            status.clear_error()
            status.set_editing("")
            self.editor = bpy.context.scene.sequence_editor
            self.strip = make_strip(length=120)
            self.view2d = FakeView2D()
            self.context, self.regions = make_editor_context(
                bpy.context.scene, self.view2d, active="WINDOW")
            self.base = float(self.strip.left_handle)
            self.channel = float(self.strip.channel)
            self.win, self.calls = _fake_window()
            self.context.window = self.win

        def tearDown(self):
            status.set_editing("")
            status.clear_error()
            hover._set_windows.clear()

        # ------------------------------------------------------------------ 工具

        def _invoke(self, local):
            """按窗口绝对坐标触发一次悬停决策，返回 operator 返回值。"""
            region = self.regions["WINDOW"]
            event = types.SimpleNamespace(
                type="MOUSEMOVE",
                mouse_x=region.x + local[0],
                mouse_y=region.y + local[1],
            )
            return _Op().invoke(self.context, event)

        def _pixel(self, rel_frame, speed):
            return geometry.point_xy(self.view2d, self.base, self.channel,
                                     rel_frame, speed)

        # ------------------------------------------------------------------ 用例

        def test_invoke_passes_through_even_over_the_line(self):
            """旁观者铁律：悬停 operator 对任何事件都 PASS_THROUGH。"""
            carrier.STORE.get_or_create(self.strip)
            self.assertEqual(self._invoke(self._pixel(60.0, 1.0)),
                             {"PASS_THROUGH"})

        def test_hover_over_the_line_requests_hand_point(self):
            """鼠标悬在速度线上：命中，光标切 HAND_POINT。"""
            carrier.STORE.get_or_create(self.strip)
            self._invoke(self._pixel(60.0, 1.0))
            self.assertEqual(self.calls, ["HAND_POINT"])

        def test_hover_off_the_line_releases_the_cursor(self):
            """从线上移开：恢复 DEFAULT；状态未变时不得重复改光标。"""
            carrier.STORE.get_or_create(self.strip)
            self._invoke(self._pixel(60.0, 1.0))
            self._invoke((10.0, 10.0))
            self.assertEqual(self.calls, ["HAND_POINT", "DEFAULT"])

            self._invoke((10.0, 10.0))
            self.assertEqual(self.calls, ["HAND_POINT", "DEFAULT"],
                             "状态未变时不得重复设置")

        def test_no_curve_no_cursor_only_when_invisible(self):
            """可见 = 可抓（ADR-0005 决定 2）：悬停命中跟着可见集合走。

            未选中且没建线的条带没有可见的线 → 不命中；选中（默认线可见）
            → 命中（按下去本来就能抓默认线建曲线）。
            """
            self.strip.select = False
            self.editor.active_strip = None
            self._invoke(self._pixel(60.0, 1.0))
            self.assertEqual(self.calls, [],
                             "看不见的默认线不许悬停命中")

            self.strip.select = True
            self._invoke(self._pixel(60.0, 1.0))
            self.assertEqual(self.calls, ["HAND_POINT"],
                             "选中后默认线可见，悬停应命中")

        def test_hover_is_suspended_while_dragging(self):
            """拖动中（editing 非空）：悬停完全不碰光标。"""
            status.set_editing(self.strip.name)
            carrier.STORE.get_or_create(self.strip)
            self._invoke(self._pixel(60.0, 1.0))
            self.assertEqual(self.calls, [])

        def test_polling_does_not_pollute_the_hit_diagnostic(self):
            """悬停不许冲刷面板"最近一次命中"（那是"按下"的语义）。"""
            carrier.STORE.get_or_create(self.strip)
            status.note_hit("SEGMENT")  # 模拟此前某次按下的诊断位
            self._invoke(self._pixel(60.0, 1.0))
            self.assertEqual(status.STATE["last_hit"], "SEGMENT",
                             "悬停不得覆写命中诊断")

        def test_non_mousemove_events_are_ignored(self):
            """F3/脚本直调（event=None 或非 MOUSEMOVE）：不动光标。"""
            carrier.STORE.get_or_create(self.strip)
            op = _Op()
            self.assertEqual(op.invoke(self.context, None), {"PASS_THROUGH"})
            press = types.SimpleNamespace(type="LEFTMOUSE", value="PRESS",
                                          mouse_x=100.0, mouse_y=100.0)
            self.assertEqual(op.invoke(self.context, press), {"PASS_THROUGH"})
            self.assertEqual(self.calls, [])

        def test_no_sequencer_area_is_a_noop(self):
            """窗口上下文里没有 VSE：不命中、不改光标。"""
            self.context.area = types.SimpleNamespace(
                type="VIEW_3D", regions=[])
            self.context.screen = types.SimpleNamespace(
                areas=[self.context.area])
            carrier.STORE.get_or_create(self.strip)
            self._invoke((50.0, 50.0))
            self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
