# -*- coding: utf-8 -*-
"""`regions` 的单元测试：**操作符必须自己找到时间轴区域**。

回归的根因（真机 5.2.1 实测）：

- ``region.view2d`` 在**所有**区域类型上都不是 None（HEADER / UI / TOOLS /
  CHANNELS / FOOTER / PREVIEW 全都有，连 1x1 的折叠区域也有），所以
  "拿不到就放弃"的防御永远不会触发，换来的是一个**坐标系不符**的 view2d；
- 同一个鼠标像素、同一条曲线，只用换 view2d 来源：

  WINDOW → ``Hit(SEGMENT, 0)``；UI → ``Hit(NONE)``（``region_to_view`` 是
  ``(nan, nan)``）；TOOLS / CHANNELS → ``Hit(NONE)``（y ≈ -266）。

- 三条入口的 ``context.region`` 分别是 UI（面板按钮）、UI（鼠标停在面板上的
  Alt+S）、TOOLS（工具栏按钮）——**没有一个是时间轴**。

于是"速度线画得出来（绘制走 WINDOW/POST_PIXEL 回调，region 必然正确），
但怎么点都没反应"。本文件把"取对区域"这件事钉死。

纯 Python，无 bpy。
"""

import unittest

from seqvel import i18n, regions

from support import (TIMELINE_ORIGIN, FakeArea, FakeRegion, DeadView2D,
                     FakeView2D, make_editor_context, make_event)


def _context(active="WINDOW", view2d=None):
    return make_editor_context(None, view2d or FakeView2D(), active=active)


class TestTimelineRegion(unittest.TestCase):
    def test_uses_context_region_when_it_is_the_timeline(self):
        context, found = _context("WINDOW")
        self.assertIs(regions.timeline_region(context), found["WINDOW"])

    def test_finds_the_timeline_even_when_context_region_is_the_sidebar(self):
        """面板按钮 / 侧栏里的 Alt+S：context.region 是 UI，也得找到 WINDOW。"""
        context, found = _context("UI")
        self.assertIs(regions.timeline_region(context), found["WINDOW"])
        self.assertEqual(context.region.type, "UI", "夹具要真的让 region 是 UI")

    def test_finds_the_timeline_when_context_region_is_the_toolbar(self):
        context, found = _context("TOOLS")
        self.assertIs(regions.timeline_region(context), found["WINDOW"])

    def test_view2d_is_the_timeline_not_the_sidebar(self):
        """核心断言：拿到的 view2d 必须是时间轴那个，不是侧栏那个。"""
        context, found = _context("UI")
        self.assertIs(regions.view2d(context), found["WINDOW"].view2d)
        self.assertIsNot(regions.view2d(context), context.region.view2d)

    def test_sidebar_view2d_is_indeed_useless(self):
        """夹具本身要能表达真机事实：侧栏的 view2d 存在但换算无意义。"""
        context, found = _context("UI")
        ui_view2d = context.region.view2d
        self.assertIsNotNone(ui_view2d, "侧栏的 view2d 不是 None（真机就是这样）")
        x, y = ui_view2d.view_to_region(1.0, 1.0, clip=False)
        self.assertNotEqual(x, x, "应为 nan：%r" % (x,))
        self.assertNotEqual(y, y, "应为 nan：%r" % (y,))

    def test_no_editor_area_means_no_region(self):
        import types

        context = types.SimpleNamespace(scene=None, area=None, region=None,
                                        screen=types.SimpleNamespace(areas=[]))
        self.assertIsNone(regions.timeline_region(context))
        self.assertIsNone(regions.view2d(context))

    def test_falls_back_to_screen_areas(self):
        """在别的编辑器里调用（context.area 不是 VSE）时从 screen 里找。"""
        import types

        area = FakeArea(x=2, y=23, width=1574, height=888)
        window = area.add(FakeRegion("WINDOW", x=TIMELINE_ORIGIN[0],
                                     y=TIMELINE_ORIGIN[1], width=800, height=600,
                                     view2d=FakeView2D()))
        info = FakeArea(kind="INFO")
        context = types.SimpleNamespace(
            scene=None, area=info, region=None,
            screen=types.SimpleNamespace(areas=[info, area]))
        self.assertIs(regions.timeline_region(context), window)

    def test_tiny_collapsed_region_is_rejected(self):
        """1x1 的折叠区域没有可用数据：宁可报"找不到"，也不拿它硬算。"""
        import types

        area = FakeArea()
        area.add(FakeRegion("WINDOW", x=10, y=10, width=1, height=1,
                            view2d=DeadView2D()))
        context = types.SimpleNamespace(scene=None, area=area, region=None)
        self.assertIsNone(regions.timeline_region(context))

    def test_region_label_explains_why(self):
        # region_label 是运行层文案（ADR-0009）：默认 en，钉 zh 验证中文版
        import types

        i18n.set_language_for_test("zh")
        try:
            context, _ = _context("UI")
            self.assertIn("时间轴区域", regions.region_label(context))

            no_area = types.SimpleNamespace(scene=None, area=None)
            self.assertIn("找不到", regions.region_label(no_area))

            info = FakeArea(kind="INFO")
            other = types.SimpleNamespace(scene=None, area=info)
            self.assertIn("INFO", regions.region_label(other))
        finally:
            i18n.set_language_for_test(None)


class TestMouseCoordinates(unittest.TestCase):
    """鼠标坐标必须换算到**时间轴区域**坐标系。"""

    def test_window_coordinates_are_converted_into_the_timeline(self):
        context, found = _context("UI")
        local = (120.0, 400.0)
        event = make_event("LEFTMOUSE",
                           local=(0.0, 0.0),  # 故意给个无关值（别的区域派发的）
                           window=(TIMELINE_ORIGIN[0] + local[0],
                                   TIMELINE_ORIGIN[1] + local[1]))
        self.assertEqual(regions.mouse(event, context), local)
        self.assertEqual(found["WINDOW"].x, TIMELINE_ORIGIN[0])

    def test_falls_back_to_region_coordinates_without_window_ones(self):
        context, _ = _context("WINDOW")
        event = make_event("LEFTMOUSE", local=(33.0, 44.0))
        self.assertEqual(regions.mouse(event, context), (33.0, 44.0))

    def test_no_event_means_no_mouse(self):
        context, _ = _context("WINDOW")
        self.assertIsNone(regions.mouse(None, context))

    def test_event_without_any_coordinates_is_none(self):
        import types

        context, _ = _context("WINDOW")
        event = types.SimpleNamespace(type="LEFTMOUSE", value="PRESS")
        self.assertIsNone(regions.mouse(event, context))


if __name__ == "__main__":
    unittest.main()
