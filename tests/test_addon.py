# -*- coding: utf-8 -*-
"""插件装配、操作符与绘制层的 headless 单测。

接缝 3（overlay + modal）按规格以人工验证为准，但其中"不依赖 GPU 的部分"
——绘制回调的容错、绘制原语调用序列、操作符的拒绝路径与删除路径——可以
在这里自动回归。
"""

import types
import unittest

from seqvel.curve import CURVE_TYPES, FAST, SMOOTH, Curve

from support import (HAVE_BPY, TIMELINE_ORIGIN, DeadView2D, FakeArea, FakeRegion,
                     FakeView2D, make_editor_context, make_event, make_strip,
                     reset_scene)

# bpy 相关模块只在有 bpy 时导入：纯 Python 下本模块能被 import（用例整体 skip），
# 而不是让 discovery 直接报 import 错误。
if HAVE_BPY:
    import bpy

    from seqvel import (VERSION_TAG, carrier, geometry, interaction, i18n,
                        overlay, status, store, ui)
    from seqvel.store import PROP_KEY


def setUpModule():
    # 本文件大量断言中文面板文案（ADR-0009：运行层默认 en，中文经 i18n 提供）
    if HAVE_BPY:
        i18n.set_language_for_test("zh")


def tearDownModule():
    if HAVE_BPY:
        i18n.set_language_for_test(None)


class _DrawRecorder(object):
    """把绘制原语换成记录器（绕开 GPU），用于验证"画了什么"。"""

    def __init__(self, patch_hud=True):
        self.calls = []
        self.patch_hud = patch_hud
        self._saved = {}

    def __enter__(self):
        self._saved["line"] = overlay._draw_polyline
        self._saved["square"] = overlay._draw_square
        self._saved["hud"] = overlay._draw_hud
        overlay._draw_polyline = (
            lambda shader, points, color, width=2.0: self.calls.append(("line", list(points))))
        overlay._draw_square = (
            lambda shader, center, size, color: self.calls.append(("square", center)))
        overlay._draw_hud = (
            lambda text, position: self.calls.append(("hud", text)))
        return self

    def __exit__(self, *_exc):
        overlay._draw_polyline = self._saved["line"]
        overlay._draw_square = self._saved["square"]
        overlay._draw_hud = self._saved["hud"]
        return False

    def of(self, kind):
        return [call[1] for call in self.calls if call[0] == kind]


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestRegistration(unittest.TestCase):
    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        import seqvel

        self.seqvel = seqvel

    def tearDown(self):
        try:
            self.seqvel.unregister()
        except Exception:
            pass

    def test_register_wires_everything_up(self):
        self.seqvel.register()
        scene = bpy.context.scene
        self.assertIsNotNone(getattr(scene, "seqvel", None), "场景应挂上插件设置")
        scene.seqvel.show_hud = "OFF"
        self.assertEqual(scene.seqvel.show_hud, "OFF")
        self.assertIsNotNone(getattr(scene.seqvel, "hide_native_retiming", None))
        self.assertIsNotNone(overlay._handle, "应挂上绘制回调")
        self.assertIn("按住", status.STATE["hint"],
                      "空闲提示要说明直接操纵的用法：%r" % status.STATE["hint"])
        self.assertIsNotNone(bpy.types.SEQVEL_PT_speedline)
        self.assertIsNotNone(getattr(bpy.types, "SEQVEL_OT_hover_cursor", None),
                             "悬停光标 operator 应随插件注册")

    def test_register_is_idempotent_after_unregister(self):
        self.seqvel.register()
        self.seqvel.unregister()
        self.assertFalse(hasattr(bpy.types, "SEQVEL_OT_hover_cursor"),
                         "注销后悬停 operator 必须摘掉")
        self.seqvel.register()
        self.assertIsNotNone(getattr(bpy.context.scene, "seqvel", None))
        self.assertIsNotNone(overlay._handle)

    def test_unregister_cleans_up(self):
        self.seqvel.register()
        self.seqvel.unregister()
        self.assertFalse(hasattr(bpy.types.Scene, "seqvel"))
        self.assertIsNone(overlay._handle)
        addons = bpy.context.window_manager.keyconfigs.addon
        self.assertIsNone(addons.keymaps.get("Sequencer"))

    def test_keymap_is_registered(self):
        """四个键位：左键拖线（普通+Ctrl 微调，R9）、右键点菜单、MOUSEMOVE 悬停。"""
        self.seqvel.register()
        addons = bpy.context.window_manager.keyconfigs.addon
        keymap = addons.keymaps.get("Sequencer")
        self.assertIsNotNone(keymap)
        by_id = {}
        for item in keymap.keymap_items:
            by_id.setdefault(item.idname, []).append(item)
        drag = by_id.get("seqvel.edit_speedline", [])
        self.assertEqual(sorted((item.type, item.value, bool(item.ctrl))
                                for item in drag),
                         [("LEFTMOUSE", "PRESS", False),
                          ("LEFTMOUSE", "PRESS", True)],
                         "普通与 Ctrl 微调（R9）两个变体都必须注册：%r" % (drag,))
        # head=True 逐条插表头：后注册的 ctrl 变体反而排在最前，所以上面的
        # 断言必须与顺序无关（键位匹配只要两条都压在原生同键项之前即可）。
        menu = by_id.get("seqvel.point_menu", [])
        self.assertEqual(len(menu), 1, "右键键位必须注册（点菜单入口）")
        self.assertEqual(menu[0].type, "RIGHTMOUSE")
        hover_items = by_id.get("seqvel.hover_cursor", [])
        self.assertEqual([(item.type, item.value) for item in hover_items],
                         [("MOUSEMOVE", "ANY")],
                         "悬停只能靠事件驱动（Window 无 mouse_x 可轮询）")
        self.assertEqual(by_id.get("seqvel.delete_point", []),
                         [], "D 键位已移除（R2：菜单打开期间键位表停摆）")
        self.assertEqual([item for item in keymap.keymap_items
                          if item.type == "S" and item.alt],
                         [], "Alt+S 入口已随会话一起移除")

    def test_keymap_registration_is_self_healing(self):
        """重复注册不累积键位项（注册前清残留）。"""
        self.seqvel.register()
        self.seqvel.unregister()
        self.seqvel.register()
        addons = bpy.context.window_manager.keyconfigs.addon
        keymap = addons.keymaps.get("Sequencer")
        ids = [(item.idname, item.type) for item in keymap.keymap_items]
        self.assertEqual(len([i for i in ids if i[0] == "seqvel.edit_speedline"]),
                         2, "重复注册不得累积键位项（普通+Ctrl 共两条）：%r" % (ids,))

    def test_selfheal_handler_survives_a_file_load(self):
        """[回归] 「载入后自愈键位」必须挺过"打开 .blend"（票 04 把装饰挪到了注册期）。

        ``persistent`` 的实现就是给函数对象打标记（5.2.1 实测 ``_bpy_persistent``），
        但打标记不是目的——**真存一次、真打开一次**，看 Blender 认不认这个处理器。
        少了它，用户一打开工程文件，R8 的自愈就只剩"注册那一次"。
        """
        import os
        import tempfile

        self.seqvel.register()
        handler = self.seqvel._reregister_keymap_on_load
        self.assertIn(handler, list(bpy.app.handlers.load_post), "注册后应挂上自愈处理器")
        path = os.path.join(tempfile.gettempdir(), "seqvel_keymap_persist.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        try:
            bpy.ops.wm.open_mainfile(filepath=path)
            self.assertIn(handler, list(bpy.app.handlers.load_post),
                          "打开 .blend 后自愈处理器必须还在（否则 R8 只在注册那一次有效）")
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.seqvel.unregister()
        self.assertNotIn(handler, list(bpy.app.handlers.load_post),
                         "注销后必须摘掉自愈处理器（否则下一次注册累积）")

    def test_speedline_tool_is_gone(self):
        """常驻直接操纵后，工具栏工具是冗余入口，必须不存在。"""
        from seqvel import interaction

        self.assertFalse(hasattr(interaction, "SEQVEL_TOOL_speedline"))

    def test_edit_session_is_one_undo_step(self):
        """每次拖动/插点/删除各记一步撤销（ADR-0005）；仍只声明 UNDO 选项、不逐动作压栈。"""
        from seqvel import interaction

        self.assertIn("UNDO", interaction.SEQVEL_OT_edit_speedline.bl_options)
        self.assertIn("UNDO", interaction.SEQVEL_OT_delete_speedline.bl_options)
        self.assertIn("UNDO", interaction.SEQVEL_OT_delete_point.bl_options)
        self.assertFalse(hasattr(interaction.SEQVEL_OT_edit_speedline, "_push_undo"))


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestOperators(unittest.TestCase):
    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        import seqvel

        self.seqvel = seqvel
        self.seqvel.register()

    def tearDown(self):
        try:
            self.seqvel.unregister()
        except Exception:
            pass

    def test_edit_operator_leaves_no_trace_without_a_region(self):
        """后台没有 SEQUENCE_EDITOR 区域：不得写入任何数据。"""
        strip = make_strip(length=120)
        bpy.context.scene.sequence_editor.active_strip = strip
        try:
            bpy.ops.seqvel.edit_speedline("INVOKE_DEFAULT")
        except RuntimeError:
            pass  # 报错退出（无区域）也是可接受的结果
        self.assertNotIn(PROP_KEY, strip.keys(), "取消不应产生任何数据")
        self.assertIsNone(carrier.carrier_of(strip))
        self.assertIsNone(store.STORE.get(strip))

    def test_delete_operator_removes_curve_and_carrier(self):
        strip = make_strip(length=120)
        bpy.context.scene.sequence_editor.active_strip = strip
        curve = store.STORE.get_or_create(strip)
        curve.insert_point(50.0, 1.6)
        store.STORE.commit(strip, curve)
        carrier.write_curve(strip, curve)
        self.assertIsNotNone(carrier.carrier_of(strip))

        result = bpy.ops.seqvel.delete_speedline()
        self.assertEqual(result, {"FINISHED"})
        self.assertNotIn(PROP_KEY, strip.keys())
        self.assertIsNone(carrier.carrier_of(strip))
        self.assertIsNone(store.STORE.get(strip))

    def test_delete_operator_without_selection_reports_error(self):
        bpy.context.scene.sequence_editor.active_strip = None
        with self.assertRaises(RuntimeError):
            bpy.ops.seqvel.delete_speedline()

    def test_audio_strip_is_refused_by_edit_operator(self):
        from support import make_sound_strip

        sound = make_sound_strip()
        if sound is None:
            self.skipTest("本机无法创建音频条带")
        bpy.context.scene.sequence_editor.active_strip = sound
        try:
            bpy.ops.seqvel.edit_speedline("INVOKE_DEFAULT")
        except RuntimeError:
            pass  # 被拒并报错退出
        self.assertIsNone(carrier.carrier_of(sound))
        self.assertEqual(carrier.delete_carriers(sound), 0)


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestOverlay(unittest.TestCase):
    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        self.strip = make_strip(length=120)

    def test_callback_is_safe_without_area(self):
        status.STATE["draw_calls"] = 0
        overlay._callback()  # 后台无 region：必须静默返回
        self.assertEqual(status.STATE["draw_calls"], 1)
        self.assertIn("时间轴", status.STATE["draw_reason"])

    def test_draw_strip_emits_expected_primitives(self):
        curve = Curve([(0.0, 1.0), (50.0, 1.5), (120.0, 1.0)], 120.0)
        with _DrawRecorder() as recorder:
            self.assertTrue(overlay.draw_strip(FakeView2D(), self.strip, curve, object()))
        self.assertEqual([len(points) for points in recorder.of("line")], [3, 2],
                         "速度线（3 点）+ 1.0x 基线（2 点）")
        self.assertEqual(len(recorder.of("square")), 3)
        self.assertEqual(recorder.of("hud"), ["100%", "150%", "100%"])

    def test_draw_strip_adds_tail_extension_line(self):
        curve = Curve.default(120.0)
        curve.move_point(1, rel_frame=60.0)
        with _DrawRecorder() as recorder:
            overlay.draw_strip(FakeView2D(), self.strip, curve, object())
        lines = recorder.of("line")
        self.assertEqual([len(points) for points in lines], [2, 2, 2],
                         "速度线 + 尾段延长线 + 基线")
        self.assertEqual(lines[0][-1], lines[1][0], "延长线应从末点接出去")

    def test_draw_strip_honours_an_explicit_hud_selection(self):
        """HUD 的取舍由**调用方**决定（勾选全显 / 拖动临时显示那几个点）。

        `draw_strip` 只执行、不做判断——判断要读场景设置与拖动状态，那是
        `_draw_all` + `overlay.visible_hud_indices` 的事（R10）。
        """
        with _DrawRecorder() as recorder:
            overlay.draw_strip(FakeView2D(), self.strip, Curve.default(120.0),
                               object(), hud_indices=set())
        self.assertEqual(recorder.of("hud"), [], "空集合 = 一个数字都不画")
        with _DrawRecorder() as recorder:
            overlay.draw_strip(FakeView2D(), self.strip, Curve.default(120.0),
                               object(), hud_indices={0})
        self.assertEqual(recorder.of("hud"), ["100%"], "只画集合里那个点")
        self.assertEqual(len(recorder.of("square")), 2)

    def test_draw_strip_points_sit_where_geometry_says(self):
        view2d = FakeView2D()
        curve = Curve([(0.0, 1.0), (120.0, 1.0)], 120.0)
        with _DrawRecorder() as recorder:
            overlay.draw_strip(view2d, self.strip, curve, object())
        line = recorder.of("line")[0]
        self.assertAlmostEqual(line[0][1], line[1][1], "1.0x 应水平")
        self.assertAlmostEqual(line[0][0] + 120.0 * view2d.x_scale, line[1][0])

    def test_draw_strip_survives_broken_view2d(self):
        broken = FakeView2D()
        broken.broken = True
        self.assertFalse(overlay.draw_strip(broken, self.strip,
                                            Curve.default(120.0), object()))
        with self.assertRaises(RuntimeError):
            overlay.draw_strip(None, self.strip, Curve.default(120.0), object())

    def test_draw_all_refuses_to_use_a_non_timeline_region(self):
        """只有侧栏区域（view2d 存在但无意义）时，必须报告"找不到时间轴区域"。

        回归根因的绘制侧护栏：绘制回调的 region 本来必然正确，但一旦有人把
        ``context.region.view2d`` 引进来，这条断言会立刻红。
        """
        area = FakeArea()
        area.add(FakeRegion("UI", x=1575, y=95, width=300, height=862,
                            view2d=DeadView2D()))
        context = types.SimpleNamespace(scene=bpy.context.scene, area=area,
                                        region=area.regions[0])
        self.assertIn("时间轴", overlay._draw_all(context))

    def testvisible_strips_only_source_and_selected(self):
        editor = bpy.context.scene.sequence_editor
        self.strip.select = True
        effect = carrier.ensure_carrier(self.strip)
        effect.select = True
        context = types.SimpleNamespace(scene=bpy.context.scene)
        self.assertEqual(overlay.visible_strips(context), [self.strip])

    def testvisible_strips_includes_active_even_unselected(self):
        editor = bpy.context.scene.sequence_editor
        self.strip.select = False
        editor.active_strip = self.strip
        context = types.SimpleNamespace(scene=bpy.context.scene)
        self.assertEqual(overlay.visible_strips(context), [self.strip])

    def testvisible_strips_keeps_curved_strip_visible_unselected(self):
        """常开显示（ADR-0005）：建过速度曲线的条带不选中也要画。"""
        editor = bpy.context.scene.sequence_editor
        self.strip.select = False
        editor.active_strip = None
        carrier.STORE.get_or_create(self.strip)  # 只建内存模型，未落盘也算"有线"
        context = types.SimpleNamespace(scene=bpy.context.scene)
        self.assertEqual(overlay.visible_strips(context), [self.strip])

    def testvisible_strips_hides_plain_unselected_strip(self):
        """没建过线又没选中的条带不可见——不存在隐形可抓的线。"""
        editor = bpy.context.scene.sequence_editor
        self.strip.select = False
        editor.active_strip = None
        context = types.SimpleNamespace(scene=bpy.context.scene)
        self.assertEqual(overlay.visible_strips(context), [])

    def test_redraw_and_header_are_silent_without_area(self):
        overlay.redraw(bpy.context)
        overlay.set_header_text(bpy.context, "x")
        overlay.set_header_text(bpy.context, None)


if HAVE_BPY:

    class _OperatorStub(object):
        """操作符替身：复用真实方法，只把 ``report`` 收进列表。

        bpy 操作符没法直接实例化（``bpy_struct.__new__`` 有约束），但 ``invoke`` /
        ``execute`` 只用到类上的这些方法 + ``report``，用鸭子类型调用即可。
        方法集必须与 test_session 的 ``_DRAG_METHODS`` 同步——缺一个，
        ``_start`` 就会在真机风格用例里 AttributeError → 被当作失败穿透。
        """

        _view2d = interaction.SEQVEL_OT_edit_speedline._view2d
        _base_frame = interaction.SEQVEL_OT_edit_speedline._base_frame
        _channel = interaction.SEQVEL_OT_edit_speedline._channel
        _mouse = interaction.SEQVEL_OT_edit_speedline._mouse
        _cleanup = interaction.SEQVEL_OT_edit_speedline._cleanup
        _start = interaction.SEQVEL_OT_edit_speedline._start
        _begin_drag = interaction.SEQVEL_OT_edit_speedline._begin_drag
        _end_action = interaction.SEQVEL_OT_edit_speedline._end_action
        _apply_drag = interaction.SEQVEL_OT_edit_speedline._apply_drag
        _release_without_drag = interaction.SEQVEL_OT_edit_speedline._release_without_drag
        _insert_point = interaction.SEQVEL_OT_edit_speedline._insert_point
        _commit = interaction.SEQVEL_OT_edit_speedline._commit
        _finish = interaction.SEQVEL_OT_edit_speedline._finish
        _capture_selection = interaction.SEQVEL_OT_edit_speedline._capture_selection
        _restore_selection = interaction.SEQVEL_OT_edit_speedline._restore_selection
        _retune = interaction.SEQVEL_OT_edit_speedline._retune
        _header_text = interaction.SEQVEL_OT_edit_speedline._header_text
        _drag_indices = interaction.SEQVEL_OT_edit_speedline._drag_indices

        def __init__(self):
            self.reports = []
            self.strip = None
            self.curve = None

        def report(self, level, message):
            self.reports.append((set(level), message))


def _event(kind, value="PRESS"):
    return types.SimpleNamespace(type=kind, value=value,
                                 mouse_region_x=0.0, mouse_region_y=0.0)


class _PanelLayout(object):
    """UILayout 替身：记录标签、每个 operator 的 operator_context，以及属性名。"""

    def __init__(self):
        self.labels = []
        self.operators = []
        self.props = []
        self.operator_context = "EXEC_DEFAULT"

    def box(self):
        return self

    def row(self, align=False):
        return self

    def column(self, heading=None):
        return self

    def separator(self):
        return None

    def label(self, text="", icon=None):
        self.labels.append(text or "")

    def prop(self, owner, name, **kwargs):
        self.props.append((owner, name))
        return None

    def operator(self, idname, text=None):
        self.operators.append((idname, self.operator_context))
        return None


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestEntryPaths(unittest.TestCase):
    """直接操纵的返回约定（ADR-0005）。

    回归点：编辑操作符挂在 LEFTMOUSE 键位上，**任何**一次没按中的点击都必须
    ``PASS_THROUGH``——只要有一次返回 ``CANCELLED``，这次点击就被吃掉，用户
    点条带选不中、框选框不动。这是直接操纵模式的生死线。
    """

    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        status.clear_error()
        status.set_editing("")
        self.editor = bpy.context.scene.sequence_editor

    def tearDown(self):
        status.clear_error()
        status.set_editing("")
        interaction._CLICKS.forget()

    def _deselect_all(self):
        for strip in self.editor.strips:
            strip.select = False
        self.editor.active_strip = None

    def _context(self):
        return types.SimpleNamespace(scene=bpy.context.scene, region=None, area=None)

    def _gui_context(self, mouse=(0.0, 0.0)):
        """真机风格的假上下文：让按下能走完整流程（不碰 GPU）。

        ``active="WINDOW"`` 表示"鼠标点在时间轴上"；
        ``area.regions`` 里同时有 UI / TOOLS 两个"view2d 存在但坐标系不符"的
        区域，若代码误取 ``context.region.view2d``，命中检测必落空。
        """
        view2d = FakeView2D()
        context, _regions = make_editor_context(bpy.context.scene, view2d,
                                                active="WINDOW")
        event = make_event("LEFTMOUSE", local=mouse,
                           window=(TIMELINE_ORIGIN[0] + mouse[0],
                                   TIMELINE_ORIGIN[1] + mouse[1]))
        return context, event, view2d

    def test_mouse_click_without_target_passes_through(self):
        self._deselect_all()
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            op, self._context(), _event("LEFTMOUSE"))
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(op.reports, [], "穿透路径不该弹报错（报错即事件被吃掉）")

    def test_keyboard_event_without_mouse_passes_through(self):
        """操作符只挂在鼠标键位上；其它事件进来一律穿透，绝不消费。"""
        self._deselect_all()
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            op, self._context(), _event("S"))
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(op.reports, [])

    def test_click_on_audio_strip_passes_through(self):
        """音频条带没有速度线：点它就是原生的选择操作。"""
        from support import make_sound_strip

        sound = make_sound_strip()
        if sound is None:
            self.skipTest("本机无法创建音频条带")
        self._deselect_all()
        sound.select = True
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            op, self._context(), _event("LEFTMOUSE"))
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(op.reports, [])
        self.assertFalse(status.STATE["editing"])

    def test_mouse_click_away_from_the_curve_passes_through(self):
        strip = make_strip(length=120)
        self._deselect_all()
        strip.select = True
        self.editor.active_strip = strip
        op = _OperatorStub()
        context, event, _ = self._gui_context(mouse=(99999.0, 99999.0))
        result = interaction.SEQVEL_OT_edit_speedline.invoke(op, context, event)
        self.assertEqual(result, {"PASS_THROUGH"},
                         "按在空白处要穿透给原生框选/点选")
        self.assertFalse(status.STATE["editing"], "穿透时不该进入编辑态")

    def test_operator_stub_covers_every_method(self):
        """替身方法集必须与 test_session 的 `_DRAG_METHODS` 一样完整。

        缺一个，`_start` 就会在真机风格用例里 AttributeError → 被 invoke 的容错
        吞成 `PASS_THROUGH`，表现为"按了没反应"的**假红**（v1.2.9 真机踩过）。
        gui_smoke 里有同款检查，这里是第三处、也是最容易被忘的一处。
        """
        real = {name for name in vars(interaction.SEQVEL_OT_edit_speedline)
                if not name.startswith("__")
                and callable(getattr(interaction.SEQVEL_OT_edit_speedline, name))}
        borrowed = {name for name in vars(_OperatorStub)
                    if callable(getattr(_OperatorStub, name))}
        # invoke / execute / modal / _modal / cancel 是**入口**：用例直接调到真实类上，
        # 替身借的是 `_start` 及其下游（那才是 `_start` 会顺着调下去的一串）。
        entries = {"invoke", "execute", "modal", "_modal", "cancel"}
        self.assertEqual(sorted(real - borrowed - entries), [],
                         "替身漏了方法，补进 _OperatorStub")

    def test_mouse_click_on_the_curve_enters_modal_drag(self):
        strip = make_strip(length=120)
        self._deselect_all()
        strip.select = True
        self.editor.active_strip = strip
        op = _OperatorStub()
        context, _unused, view2d = self._gui_context()
        # 把鼠标放到第一个速度点（0 帧、1.0x）上。事件要按真机语义给全：
        # 窗口绝对坐标与区域局部坐标都给，否则测的是"半真"的事件。
        x, y = geometry.point_xy(view2d, float(strip.left_handle), float(strip.channel),
                                 0.0, 1.0)
        event = make_event("LEFTMOUSE", local=(x, y),
                           window=(TIMELINE_ORIGIN[0] + x, TIMELINE_ORIGIN[1] + y))
        result = interaction.SEQVEL_OT_edit_speedline.invoke(op, context, event)
        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(status.STATE["editing"], strip.name)
        op._cleanup(context)

    def test_mouse_path_never_cancels_even_without_a_screen(self):
        """鼠标路径在任何失败原因下都不许 CANCELLED——包括"拿不到 region"。"""
        strip = make_strip(length=120)
        self._deselect_all()
        strip.select = True
        self.editor.active_strip = strip
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            op, self._context(), _event("LEFTMOUSE"))
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(op.reports, [], "鼠标路径不该用报错的方式消费事件")

    def test_execute_never_succeeds_silently(self):
        """F3 / 脚本走 execute：必须有明确提示，而不是静默无反应。"""
        strip = make_strip(length=120)
        self._deselect_all()
        strip.select = True
        self.editor.active_strip = strip
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.execute(op, self._context())
        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(len(op.reports), 1)

@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestSegmentTypeSummary(unittest.TestCase):
    """面板的段类型分布（票 03）：改完类型必须看得见，没数据也要说明。"""

    def test_summary_lists_all_four_types(self):
        curve = Curve([(0.0, 1.0), (40.0, 2.0), (100.0, 1.0)], 100,
                      types=["smooth", "smooth"])
        text = ui.segment_type_summary(curve)
        for label in ("线性", "平滑", "快速", "慢速"):
            self.assertIn(label, text)
        self.assertIn("平滑 2", text)

    def test_summary_without_a_model_says_so(self):
        self.assertIn("还没有速度线数据", ui.segment_type_summary(None))


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestPanel(unittest.TestCase):
    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        status.clear_error()
        status.set_editing("")
        self.layout = _PanelLayout()
        self.panel = types.SimpleNamespace(layout=self.layout)

    def _draw(self):
        ui.SEQVEL_PT_speedline.draw(
            self.panel, types.SimpleNamespace(scene=bpy.context.scene))
        return self.layout

    def test_panel_explains_why_there_is_no_target(self):
        strip = make_strip(length=120)
        strip.select = False
        bpy.context.scene.sequence_editor.active_strip = None
        layout = self._draw()
        self.assertIn("目标条带：（无）", layout.labels)
        self.assertTrue(any("选中 0 条" in text for text in layout.labels),
                        "面板要说清为什么没有目标：%r" % (layout.labels,))

    def test_panel_shows_strip_without_an_edit_button(self):
        """直接操纵后没有「编辑」按钮了：只剩删除，提示改讲"按住速度线"。"""
        strip = make_strip(length=120)
        strip.select = True
        bpy.context.scene.sequence_editor.active_strip = strip
        layout = self._draw()
        self.assertIn("目标条带：%s" % strip.name, layout.labels)
        contexts = dict(layout.operators)
        self.assertNotIn("seqvel.edit_speedline", contexts,
                         "「编辑」按钮已随会话移除（ADR-0005）")
        self.assertIn("seqvel.delete_speedline", contexts)
        self.assertTrue(any("按住" in text for text in layout.labels),
                         "面板要有常驻操作提示：%r" % (layout.labels,))

    def test_panel_shows_segment_type_distribution(self):
        """段类型必须在面板上看得见（否则"右键选了却没生效"没法自查）。"""
        strip = make_strip(length=120)
        strip.select = True
        bpy.context.scene.sequence_editor.active_strip = strip
        layout = self._draw()
        self.assertTrue(any(text.startswith("段类型") for text in layout.labels),
                        "面板应有段类型行：%r" % (layout.labels,))
        self.assertTrue(any("还没有速度线数据" in text for text in layout.labels),
                        "没有曲线时要说明，不能只留一个空值：%r" % (layout.labels,))

    def test_panel_explains_a_non_editable_target(self):
        from support import make_sound_strip

        sound = make_sound_strip()
        if sound is None:
            self.skipTest("本机无法创建音频条带")
        sound.select = True
        bpy.context.scene.sequence_editor.active_strip = sound
        layout = self._draw()
        self.assertIn("不能编辑", layout.labels)
        self.assertTrue(any("音频" in text or "源条带" in text for text in layout.labels),
                        "应说明原因：%r" % (layout.labels,))

    def test_panel_shows_running_version(self):
        """面板要显示版本号。

        覆盖安装插件后不重启 Blender，跑的还是内存里的旧模块；此时面板里的
        版本号就是"装没装上"的唯一直接证据。
        """
        layout = self._draw()
        self.assertTrue(any(VERSION_TAG in text for text in layout.labels),
                        "面板应显示当前运行的版本：%r" % (layout.labels,))


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestRawCacheGuard(unittest.TestCase):
    """上游缺陷的面板出口：提示 + 一键开关（点了才改、可再点恢复）。

    缓存是场景级设置，插件**不自动**替用户改——所以操作符与提示必须成对出现，
    且关掉之后按钮要能再点回来。
    """

    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        import seqvel

        self.seqvel = seqvel
        seqvel.register()
        self.editor = bpy.context.scene.sequence_editor
        from support import make_scene_strip

        self.make_scene_strip = make_scene_strip
        self.layout = _PanelLayout()
        self.panel = types.SimpleNamespace(layout=self.layout)

    def tearDown(self):
        try:
            self.seqvel.unregister()
        except Exception:
            pass
        try:
            self.editor.use_cache_raw = True  # 别把场景设置带进下一个用例
        except Exception:
            pass

    def _draw(self):
        ui.SEQVEL_PT_speedline.draw(
            self.panel, types.SimpleNamespace(scene=bpy.context.scene))
        return self.layout

    def test_panel_warns_and_offers_toggle_for_scene_carrier(self):
        strip = self.make_scene_strip()
        carrier.write_curve(strip, Curve([(0.0, 0.5), (60.0, 0.5)], 60.0))
        self.editor.active_strip = strip
        strip.select = True
        self.editor.use_cache_raw = True

        layout = self._draw()
        self.assertTrue(any("无级受限" in text for text in layout.labels),
                        layout.labels)
        self.assertTrue(any("Raw 缓存把子帧取整" in text for text in layout.labels),
                        layout.labels)
        self.assertIn(("seqvel.toggle_raw_cache", "EXEC_DEFAULT"), layout.operators)

    def test_panel_shows_restored_state_with_toggle_back(self):
        """关掉缓存后：状态行转为"生效"，按钮变成"打开"（可再点恢复）。"""
        strip = self.make_scene_strip()
        carrier.write_curve(strip, Curve([(0.0, 0.5), (60.0, 0.5)], 60.0))
        self.editor.active_strip = strip
        strip.select = True
        self.editor.use_cache_raw = False

        layout = self._draw()
        self.assertTrue(any("子帧渲染生效" in text for text in layout.labels),
                        layout.labels)
        self.assertFalse(any("无级受限" in text for text in layout.labels))
        self.assertIn(("seqvel.toggle_raw_cache", "EXEC_DEFAULT"), layout.operators)

    def test_toggle_operator_turns_it_off_and_back_on(self):
        self.assertTrue(hasattr(bpy.types, "SEQVEL_OT_toggle_raw_cache"),
                        "操作符必须注册，否则面板按钮点了没反应")
        self.editor.use_cache_raw = True
        op = types.SimpleNamespace(report=lambda *args: None)
        self.assertEqual(ui.SEQVEL_OT_toggle_raw_cache.execute(op, bpy.context),
                         {"FINISHED"})
        self.assertFalse(self.editor.use_cache_raw)
        self.assertEqual(ui.SEQVEL_OT_toggle_raw_cache.execute(op, bpy.context),
                         {"FINISHED"})
        self.assertTrue(self.editor.use_cache_raw)

    def test_panel_stays_quiet_for_movie_carrier(self):
        """取整选帧的素材不吃子帧：不该弹"无级受限"，也不该出现开关。"""
        strip = make_strip(length=120)
        carrier.write_curve(strip, Curve([(0.0, 1.5), (120.0, 1.5)], 120.0))
        self.editor.active_strip = strip
        strip.select = True
        self.editor.use_cache_raw = True

        layout = self._draw()
        self.assertFalse(any("无级受限" in text for text in layout.labels),
                         layout.labels)
        self.assertNotIn("seqvel.toggle_raw_cache",
                         [name for name, _ctx in layout.operators])


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestSceneSettings(unittest.TestCase):
    """场景设置项的出厂值：默认段曲线类型四档全开放且出厂平滑（ADR-0007）、
    显示速度百分比出厂**不勾选**（ADR-0005 修订 R10）。"""

    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        import seqvel

        self.seqvel = seqvel
        self.seqvel.register()
        self.strip = make_strip(length=120)

    def tearDown(self):
        try:
            self.seqvel.unregister()
        except Exception:
            pass
        store.STORE.invalidate()

    def _enum_items(self):
        # v1.5.0 起枚举项走运行时回调（ADR-0009 双语）：bl_rna.enum_items 是空的，
        # 直接调回调（它的返回就是真机 UI 拿到的东西）：((id, name, desc), ...)
        settings = bpy.context.scene.seqvel
        return list(ui._curve_type_items(settings, None))

    def test_offers_all_four_types_and_defaults_to_smooth(self):
        settings = bpy.context.scene.seqvel
        self.assertEqual([item[0] for item in self._enum_items()],
                         list(CURVE_TYPES),
                         "四档全开放，且 identifier 就是持久化用的稳定短串")
        self.assertEqual(settings.default_curve_type, SMOOTH,
                         "出厂值 = 平滑：这就是 Vegas 的开箱行为")

    def test_labels_come_from_the_shared_vocabulary(self):
        self.assertEqual([item[1] for item in self._enum_items()],
                         [i18n.type_label(kind) for kind in CURVE_TYPES],
                         "与右键段菜单共用同一套档名，不另造第二套")

    def test_items_carry_no_per_item_description(self):
        """R2（ADR-0009）：项级说明必须是**空串**——它走 RNA 缓存、切语言
        不保证重算，且非空时会与属性级双语说明拼成"英中英/英中中"重复；
        tooltip 由属性级双语说明独自承担。四个设置项的回调都按此断言。
        （本版 Blender 动态枚举回调不收二元组，最少三元组——说明位只能空串。）"""
        settings = bpy.context.scene.seqvel
        for maker in (ui._hide_retiming_items, ui._show_hud_items,
                      ui._curve_type_items, ui._speed_cap_items):
            for item in maker(settings, None):
                self.assertEqual(len(item), 3,
                                 "%s 的项 %r 不是三元组（本版 Blender 不收二元组）"
                                 % (maker.__name__, item))
                self.assertEqual(item[2], "",
                                 "%s 的项 %r 带了说明——tooltip 会拼接重复"
                                 % (maker.__name__, item))

    def test_new_curve_in_this_scene_takes_the_setting(self):
        bpy.context.scene.seqvel.default_curve_type = FAST
        curve = store.STORE.get_or_create(self.strip)
        self.assertEqual(curve.types, [FAST])

    def test_show_hud_defaults_to_unchecked(self):
        """出厂不勾选：平日没有数字，只有拖动中临时显形（R10）。"""
        # v1.5.0 起是动态枚举："OFF"（拖动中显形）= 旧的未勾选
        self.assertEqual(bpy.context.scene.seqvel.show_hud, "OFF")

    def test_panel_offers_the_dropdown(self):
        self.strip.select = True
        bpy.context.scene.sequence_editor.active_strip = self.strip
        layout = _PanelLayout()
        ui.SEQVEL_PT_speedline.draw(types.SimpleNamespace(layout=layout),
                                    types.SimpleNamespace(scene=bpy.context.scene))
        self.assertIn("default_curve_type", [name for _owner, name in layout.props],
                      "面板必须放出这个下拉（它是唯一的信息出口与设置入口）：%r"
                      % (layout.props,))


if __name__ == "__main__":
    unittest.main()
