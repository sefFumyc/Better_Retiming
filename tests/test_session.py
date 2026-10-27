# -*- coding: utf-8 -*-
"""直接操纵（ADR-0005）全链路回归：按下 → 拖动 → 松手 → 落盘 → 原生承载。

**为什么单独一个文件**：命中→起拖→落盘这段链路必须有真实回归——
"按了没反应"这类缺陷（历史上出过两次）都要在这里红。每个用例都走完整的
按下→MOUSEMOVE→松手序列，并断言**影子模型、条带自定义属性、原生 Speed
Control 关键帧**三层都被改动。

与旧"编辑会话"测试的差异：没有会话了。每次按下是一个独立的短 modal，
未命中的按下必须 ``PASS_THROUGH``（原生选择/框选/空格不被打断）；拖动
期间吞掉一切事件（只放行导航与空格，R4），且**不碰选择态**（R1：拖包络
不选素材）。

不依赖 GPU / 真实窗口：region/view2d 用 ``FakeView2D`` 替身，
坐标由 ``geometry`` 自己算（与绘制同源），因此"画在哪就点哪"。
"""

import types
import unittest

from seqvel import editing
from seqvel.curve import SPEED_MAX, Curve

from support import (HAVE_BPY, TIMELINE_ORIGIN, FakeView2D, make_editor_context,
                     make_event, make_strip, reset_scene)

if HAVE_BPY:
    import bpy

    from seqvel import carrier, geometry, interaction, overlay, status, store, ui
    from seqvel.store import PROP_KEY

#: 拖动操作符需要的方法全集。**显式列出并在下面断言覆盖完整**：将来往操作符
#: 上加了新方法却忘了同步替身，会在这里立刻炸掉，而不是让某个分支静默测不到。
_DRAG_METHODS = (
    "invoke", "execute", "_start", "modal", "_modal", "cancel",
    "_begin_drag", "_end_action", "_apply_drag", "_release_without_drag",
    "_insert_point", "_commit", "_finish", "_cleanup",
    "_capture_selection", "_restore_selection",
    "_retune", "_header_text", "_drag_indices",
    "_view2d", "_base_frame", "_channel", "_mouse",
)


if HAVE_BPY:

    class _OperatorStub(object):
        """带完整方法集的替身：让短 modal 在 headless 里真正跑起来。

        bpy 操作符实例没法直接 ``__new__``，但真实方法只用到 ``self`` 上的
        状态属性与 ``report``，鸭子类型足够。
        """

        def __init__(self):
            self.reports = []

        def report(self, level, message):
            self.reports.append((set(level), message))

    for _name in _DRAG_METHODS:
        setattr(_OperatorStub, _name,
                getattr(interaction.SEQVEL_OT_edit_speedline, _name))


def _missing_drag_methods():
    wanted = set(_DRAG_METHODS)
    real = {name for name in vars(interaction.SEQVEL_OT_edit_speedline)
            if not name.startswith("__")}
    methods = {name for name in real if callable(
        getattr(interaction.SEQVEL_OT_edit_speedline, name))}
    return sorted(methods - wanted)


def _event(kind, value="PRESS", mouse=(0.0, 0.0)):
    """时间轴区域里的局部像素 → **真机风格**的事件。

    真机上事件同时带 ``mouse_region_x/y``（事件被派发到的那个区域的局部坐标）
    与 ``mouse_x/y``（窗口绝对坐标）。这里两者都按真机语义填：窗口坐标 =
    时间轴区域原点 + 局部坐标，于是"把窗口坐标换算回时间轴区域"这条路径
    在测试里**真的被执行**（而不是被替身绕过）。
    """
    return make_event(kind, value=value, local=mouse,
                      window=(TIMELINE_ORIGIN[0] + mouse[0],
                              TIMELINE_ORIGIN[1] + mouse[1]))


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestDirectDrag(unittest.TestCase):
    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        status.clear_error()
        status.set_editing("")
        self.editor = bpy.context.scene.sequence_editor
        self.strip = make_strip(length=120)
        for strip in self.editor.strips:
            strip.select = False
        self.strip.select = True
        self.editor.active_strip = self.strip
        self.base = float(self.strip.left_handle)
        self.channel = float(self.strip.channel)
        self.view2d = FakeView2D()
        # 直接操纵：按下永远发生在时间轴（WINDOW）上
        self.context, self.regions = make_editor_context(
            bpy.context.scene, self.view2d, active="WINDOW")

    def tearDown(self):
        status.clear_error()
        status.set_editing("")
        interaction._CLICKS.forget()

    # ------------------------------------------------------------------ 工具

    def _pixel(self, rel_frame, speed):
        """模型坐标 → 区域像素（与绘制同一套换算）。"""
        return geometry.point_xy(self.view2d, self.base, self.channel, rel_frame, speed)

    def _press(self, mouse):
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            op, self.context, _event("LEFTMOUSE", mouse=mouse))
        return op, result

    def _move(self, op, mouse):
        return interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, _event("MOUSEMOVE", value="NOTHING", mouse=mouse))

    def _release(self, op, mouse):
        return interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, _event("LEFTMOUSE", value="RELEASE", mouse=mouse))

    def _native_keys(self):
        """承载条带的 speed_factor 关键帧。"""
        found = carrier.carrier_of(self.strip)
        if found is None:
            return []
        curve = carrier.speed_curve(found)
        return [] if curve is None else [(kp.co[0], kp.co[1]) for kp in curve.keyframe_points]

    # ------------------------------------------------------------------ 覆盖

    def test_operator_methods_are_all_in_the_stub(self):
        """替身必须覆盖操作符的全部方法，否则链路测试会假绿。"""
        self.assertEqual(_missing_drag_methods(), [],
                         "替身漏了方法，补进 _DRAG_METHODS")

    # ------------------------------------------------------------------ 拖动

    def test_drag_a_segment_moves_the_line_and_lands_native_keys(self):
        """按住线中段往上拖：影子模型 + 自定义属性 + 原生关键帧都要变。"""
        op, result = self._press(self._pixel(60.0, 1.0))
        self.assertEqual(result, {"RUNNING_MODAL"}, "按在线上应进入模态")
        self.assertEqual(op.mode, "SEGMENT", "按住普通段应进入段拖动")
        self.assertEqual(status.STATE["editing"], self.strip.name)

        self._move(op, self._pixel(60.0, 1.5))
        self.assertEqual([round(speed, 3) for _f, speed in op.curve.points],
                         [1.5, 1.5], "段拖动应把两个端点一起抬高")

        self._release(op, self._pixel(60.0, 1.5))
        self.assertEqual([round(speed, 3) for _f, speed in op.curve.points],
                         [1.5, 1.5], "松手后曲线保持")

        stored = self.strip.get(PROP_KEY)
        self.assertIsNotNone(stored, "松手必须把曲线写进条带自定义属性")
        self.assertEqual([round(float(v), 3) for v in stored],
                         [0.0, 1.5, 120.0, 1.5])

        keys = self._native_keys()
        self.assertEqual(len(keys), 2, "应落地两个 speed_factor 关键帧：%r" % (keys,))
        self.assertEqual([round(value, 3) for _frame, value in keys], [1.5, 1.5])

    def test_sharp_corner_drag_takes_the_nearer_segment(self):
        """尖拐点附近两段都在半径内时，拖的是**更近**的那一段（回归：票 05）。

        「命中的段号」只是表象——这条链路会真的改数据，所以把整段拖动跑一遍：
        **目标段（段1）的两个端点同步平移，另一段（段0）独占的那个端点逐位不变**。
        票 05 修的正是这里：段循环原先"第一个进半径就 return"，从下标 0 起遍历，
        于是永远拖到段0（拐点左侧那一段），用户以为改的是自己指的那段。
        """
        # 1 像素/帧、深 V：端点速度 2.0、拐点 0.2（不是 0.5——标尺跨 0.09~0.95、
        # 跨度 0.86 后 0.5 的 V 太平：12.1 像素处离段0 已是 8.15 > 段半径 8，
        # 用例会退化成"只有一段在半径内"。深 V 才留得下两段都进半径的像素）。
        # 换算率 = 3.0/(0.86×60) ≈ 0.0581 倍速/像素。
        self.view2d = FakeView2D(x_scale=1.0)
        self.context, self.regions = make_editor_context(
            bpy.context.scene, self.view2d, active="WINDOW")

        model = Curve([(0.0, 2.0), (10.0, 0.2), (20.0, 2.0)],
                      float(carrier.strip_duration(self.strip)))
        carrier.STORE.commit(self.strip, model)
        pixels = geometry.curve_xy(self.view2d, self.base, self.channel, model.points)

        def _distances(point):
            return [geometry.dist_point_segment(point[0], point[1], a[0], a[1], b[0], b[1])
                    for a, b in zip(pixels, pixels[1:])]

        # 按下点取在**段1 的线上**、沿段方向离拐点 12.1 像素处：
        # - 离拐点 > 12 像素（点半径），离末点 > 12 像素 → 不会命中点；
        # - 离段0 的线 12.1×sin(40.7°) ≈ 7.9 像素 < 8（段半径）→ 两段都在
        #   半径内、段1 明显更近——「取最近」这条回归真的被行使；
        # - 光标速度 == 线上速度 == 锚点 → 拖动增量干净（4 像素就是 4 像素）。
        corner, far = pixels[1], pixels[2]
        seg_dx, seg_dy = far[0] - corner[0], far[1] - corner[1]
        seg_len = (seg_dx * seg_dx + seg_dy * seg_dy) ** 0.5
        along = 12.1
        mouse = (corner[0] + seg_dx / seg_len * along,
                 corner[1] + seg_dy / seg_len * along)

        # 布景自检：两段都在半径内，且段1 明显更近（否则用例测的不是「最近」）
        d0, d1 = _distances(mouse)
        self.assertLess(d1, geometry.LINE_RADIUS)
        self.assertLess(d0, geometry.LINE_RADIUS,
                        "段0 也要在半径内，用例才真的在测「最近」而不是「有命中」")
        self.assertLess(d1 + 0.5, d0)

        op, result = self._press(mouse)
        self.assertEqual(result, {"RUNNING_MODAL"}, "按在线上应进入模态")
        self.assertEqual(op.mode, "SEGMENT")
        self.assertEqual(op.index, 1, "抓到的必须是更近的段1，不是先遍历到的段0")

        # 往上拖 4 像素 = 4 × 3.0/(0.86×60) ≈ 0.2326 倍速：
        # 段1 两端点同步 +shift（锚点 = 按下处的线上速度，无额外跳量）
        shift = 4.0 * SPEED_MAX / (0.86 * 60.0)
        target = (mouse[0], mouse[1] + 4.0)
        self._move(op, target)
        self.assertEqual([round(frame, 1) for frame, _speed in op.curve.points],
                         [0.0, 10.0, 20.0], "段拖动不改帧")
        self.assertEqual(op.curve.points[0][1], 2.0,
                         "段0 独占的那个端点一个数都不该变——变了就是拖错了段")
        self.assertAlmostEqual(op.curve.points[1][1], 0.2 + shift, places=6)
        self.assertAlmostEqual(op.curve.points[2][1], 2.0 + shift, places=6)

        self._release(op, target)
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 2.0, 10.0, round(0.2 + shift, 3), 20.0, round(2.0 + shift, 3)],
                         "落盘的只有段1 的两个端点")
        self.assertEqual([(int(frame), round(value, 3)) for frame, value in self._native_keys()],
                         [(1, 2.0), (11, round(0.2 + shift, 3)), (21, round(2.0 + shift, 3))],
                         "原生 speed_factor 关键帧也只动段1 的两个端点")

    def test_drag_ends_the_operator(self):
        """松手即结束：没有"会话"，松手后不该残留编辑态。"""
        op, _ = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.25))
        self._release(op, self._pixel(60.0, 1.25))
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.25, 120.0, 1.25])
        self.assertEqual(status.STATE["editing"], "", "松手后回到空闲态")

    def test_release_must_return_finished(self):
        """[修订 R7 回归] 松手的 modal 返回值必须是 FINISHED。

        v1.2.5 的吞咬窗口在松手后返回 RUNNING_MODAL、收摊路径又用
        PASS_THROUGH——PASS_THROUGH 不会结束 modal，operator 变僵尸吞掉
        一切鼠标事件，时间线冻结到狂敲 ESC（真机 e2e 实锤）。松手 = 死。
        """
        op, _ = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.25))
        result = self._release(op, self._pixel(60.0, 1.25))
        self.assertEqual(result, {"FINISHED"},
                         "松手必须立刻结束 modal（PASS_THROUGH/RUNNING_MODAL "
                         "都会留僵尸 operator，冻结时间线）")

    def test_pressing_an_unselected_but_curved_strip_drags_without_selecting(self):
        """常开显示 + 不碰选择态（ADR-0005 修订 R1）：没选中的建线条带也能直接拖，
        但拖动不得设置选中/活动——Vegas 式"拖包络不选素材"。"""
        other = make_strip(length=120, name="curved", channel=2, frame_start=1)
        other.select = False
        self.editor.active_strip = None
        carrier.STORE.get_or_create(other)  # 建线（内存模型）
        base = float(other.left_handle)
        channel = float(other.channel)
        px = geometry.point_xy(self.view2d, base, channel, 60.0, 1.0)

        stub = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            stub, self.context, _event("LEFTMOUSE", mouse=px))
        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(stub.strip.name, "curved", "应命中建线的那条，而不是选中的")
        self.assertFalse(other.select, "拖动不得设置选中态（R1）")
        self.assertIsNone(self.editor.active_strip, "拖动不得设置活动条带（R1）")

        px_up = geometry.point_xy(self.view2d, base, channel, 60.0, 1.5)
        self._move(stub, px_up)
        self._release(stub, px_up)
        self.assertEqual([round(float(v), 2) for v in other.get(PROP_KEY)],
                         [0.0, 1.5, 120.0, 1.5])
        self.assertFalse(other.select, "松手后也不补选")

    def test_drag_a_point_only_moves_that_point(self):
        """先建一个中间点，再拖它：只有它变，两端不动。"""
        curve = carrier.STORE.get_or_create(self.strip)
        self.assertTrue(curve.insert_point(60.0, 1.0))
        carrier.STORE.commit(self.strip, curve)

        op, result = self._press(self._pixel(60.0, 1.0))
        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(op.mode, "POINT", "按在方块上应进入拖点")
        self.assertEqual(op.index, 1)

        self._move(op, self._pixel(90.0, 1.5))
        self.assertEqual([round(frame, 1) for frame, _s in op.curve.points],
                         [0.0, 90.0, 120.0], "拖点只改它自己的帧")
        # 标尺铺条带绘制区后（ADR-0010），光标映射的往返带 1e-16 级浮点噪声，
        # 语义判据是"点落在光标处"而非"逐位相等"
        self.assertAlmostEqual(op.curve.points[1][1], 1.5, places=6)
        self.assertEqual(op.curve.points[0][1], 1.0, "其它点不该被带动")

        self._release(op, self._pixel(90.0, 1.5))
        self.assertEqual([round(float(v), 2) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.0, 90.0, 1.5, 120.0, 1.0])

    # ------------------------------------------------------------ 临时读数（R10）

    def test_press_publishes_the_points_this_drag_will_change(self):
        """段拖：按下即公布"这一段的两端"，松手清掉（否则数字会赖着不走）。"""
        key = store.strip_key(self.strip)
        op, _result = self._press(self._pixel(60.0, 1.0))
        self.assertEqual(op.mode, "SEGMENT")
        self.assertEqual(status.drag_target(), (key, (0, 1)))
        self._release(op, self._pixel(60.0, 1.0))
        self.assertIsNone(status.drag_target())

    def test_point_drag_publishes_only_that_point(self):
        curve = carrier.STORE.get_or_create(self.strip)
        curve.insert_point(60.0, 1.4)
        carrier.STORE.commit(self.strip, curve)
        op, _result = self._press(self._pixel(60.0, 1.4))
        self.assertEqual(op.mode, "POINT")
        self.assertEqual(status.drag_target(),
                         (store.strip_key(self.strip), (1,)))
        self._release(op, self._pixel(60.0, 1.4))
        self.assertIsNone(status.drag_target())

    def test_tail_drag_publishes_the_last_point(self):
        """尾段延长线只有一个会动的端：公布末点一个。"""
        curve = carrier.STORE.get_or_create(self.strip)
        curve.move_point(1, rel_frame=60.0, speed=1.0)
        carrier.STORE.commit(self.strip, curve)
        op, _result = self._press(self._pixel(100.0, 1.0))
        self.assertEqual(op.mode, "TAIL")
        self.assertEqual(status.drag_target(),
                         (store.strip_key(self.strip), (1,)))

    def test_cleanup_redraws_so_the_readout_disappears_at_once(self):
        """收摊必须自己补一次重绘：松手路径里有一条**既不落盘也不重绘**的。

        没拖动就松手（单击）只判双击插点，不走 `_persist` 里那次重绘——不补这一下，
        临时数字会赖到下一次自然重绘才消失（实测坑，写进 ADR-0005 R10）。
        """
        calls = []
        original = overlay.redraw
        overlay.redraw = lambda context: calls.append(context)
        try:
            op, _result = self._press(self._pixel(60.0, 1.0))
            del calls[:]
            self._release(op, self._pixel(60.0, 1.0))
            self.assertTrue(calls, "收摊必须补一次重绘")
        finally:
            overlay.redraw = original
        self.assertIsNone(status.drag_target())

    def test_escape_also_clears_the_target(self):
        op, _result = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.4))
        interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, _event("ESC"))
        self.assertIsNone(status.drag_target())

    def test_double_click_insert_also_clears_the_target(self):
        """双击插点走的是"没拖动的松手"路径，同样要把目标清掉。"""
        first = _OperatorStub()
        interaction.SEQVEL_OT_edit_speedline.invoke(
            first, self.context, _event("LEFTMOUSE", mouse=self._pixel(60.0, 1.0)))
        self._release(first, self._pixel(60.0, 1.0))
        self.assertIsNone(status.drag_target())

    def test_right_click_also_clears_the_target(self):
        """[R10 验收] 右键收摊与松手/Esc 是并列的三条收摊路径，一条都不能漏。

        漏掉任意一条的后果都一样：目标留在 state 里，数字永远亮着。
        """
        op, _result = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.4))
        self.assertIsNotNone(status.drag_target(), "按下之后必须已经有目标")
        result = interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, _event("RIGHTMOUSE"))
        self.assertEqual(result, {"FINISHED"})
        self.assertIsNone(status.drag_target())

    def test_a_modal_exception_also_clears_the_target(self):
        """[R10 验收] modal 抛异常时也要清干净——异常路径最容易被漏掉。

        真抛一次：把 `_apply_drag` 换成会炸的替身，再送一条 mousemove。
        """
        op, _result = self._press(self._pixel(60.0, 1.0))

        def boom(*_a, **_k):
            raise RuntimeError("模拟拖动中途出错")

        op._apply_drag = boom
        result = interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, _event("MOUSEMOVE", mouse=self._pixel(60.0, 1.4)))
        self.assertEqual(result, {"FINISHED"}, "异常后必须收摊，不能留僵尸 modal")
        self.assertIsNone(status.drag_target())

    def test_cancel_also_clears_the_target(self):
        """`cancel()` 是 Blender 强拆 modal 时的入口，同样要清。"""
        op, _result = self._press(self._pixel(60.0, 1.0))
        interaction.SEQVEL_OT_edit_speedline.cancel(op, self.context)
        self.assertIsNone(status.drag_target())

    def test_double_click_on_a_segment_inserts_a_point(self):
        """两次快速按在同一线上：第二次松手插点（跨 operator 运行的检测）。"""
        first = _OperatorStub()
        interaction.SEQVEL_OT_edit_speedline.invoke(
            first, self.context, _event("LEFTMOUSE", mouse=self._pixel(60.0, 1.0)))
        self._release(first, self._pixel(60.0, 1.0))  # 无拖动的第一次松手

        second = _OperatorStub()
        interaction.SEQVEL_OT_edit_speedline.invoke(
            second, self.context, _event("LEFTMOUSE", mouse=self._pixel(61.0, 1.0)))
        self._release(second, self._pixel(61.0, 1.0))
        self.assertEqual([round(frame, 1) for frame, _s in second.curve.points],
                         [0.0, 61.0, 120.0], "双击应插点")
        self.assertEqual(len(self._native_keys()), 3, "插点也要同步原生关键帧")

    def test_drag_then_release_is_not_a_double_click(self):
        """拖过的松手不算连击的第一次：拖完立刻贴着线再点不应插点。"""
        op, _ = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.5))
        self._release(op, self._pixel(60.0, 1.5))

        # 线已被拖到 1.5x：贴着新线再点一次（无拖动松手）
        second = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            second, self.context, _event("LEFTMOUSE", mouse=self._pixel(60.0, 1.5)))
        self.assertEqual(result, {"RUNNING_MODAL"})
        self._release(second, self._pixel(60.0, 1.5))
        # 速度走光标映射往返（ADR-0010 后带 1e-16 级浮点噪声），按语义放宽
        self.assertEqual([frame for frame, _s in second.curve.points],
                         [0.0, 120.0], "拖后紧接的点击不该插点")
        for _frame, speed in second.curve.points:
            self.assertAlmostEqual(speed, 1.5, places=6)
        self.assertEqual(len(second.curve.points), 2)

    def test_escape_during_drag_keeps_the_drag_that_already_happened(self):
        """拖动中 Esc：已经拖出来的改动必须落盘，不能白干。"""
        op, _ = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.25))
        result = interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, _event("ESC"))
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.25, 120.0, 1.25])
        self.assertEqual(status.STATE["editing"], "")

    def test_drag_consumes_tweak_and_unknown_events(self):
        """R4：拖动期间 TWEAK/未知事件必须被吞掉（真机"接管成拖条带"的根因）。

        按下瞬间的穿透纪律只管**按下**；拖动一旦开始，事件漏出去就会被原生
        键位接住（v1.2.0 真机 bug）。TWEAK 还要被当作拖动移动处理。
        """
        op, _ = self._press(self._pixel(60.0, 1.0))
        tweak = interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context,
            _event("EVT_TWEAK_L", value="PRESS", mouse=self._pixel(60.0, 1.5)))
        self.assertEqual(tweak, {"RUNNING_MODAL"},
                         "拖动中的 TWEAK 漏出去 = 原生开始拖条带")
        self.assertEqual([round(speed, 3) for _f, speed in op.curve.points],
                         [1.5, 1.5], "TWEAK 应被当作拖动移动处理")

        unknown = interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context,
            _event("TIMER0", value="NOTHING", mouse=self._pixel(60.0, 1.5)))
        self.assertEqual(unknown, {"RUNNING_MODAL"}, "未知事件一律吞掉")

    def test_drag_passes_through_navigation_and_space(self):
        """R4：导航（平移/缩放）与空格在拖动中照常穿透；之后拖动继续、照常落盘。"""
        op, _ = self._press(self._pixel(60.0, 1.0))
        for kind in ("MIDDLEMOUSE", "WHEELUPMOUSE", "SPACE"):
            result = interaction.SEQVEL_OT_edit_speedline.modal(
                op, self.context,
                _event(kind, value="PRESS", mouse=self._pixel(60.0, 1.0)))
            self.assertEqual(result, {"PASS_THROUGH"}, "%s 应穿透" % kind)
        self._move(op, self._pixel(60.0, 1.5))
        self._release(op, self._pixel(60.0, 1.5))
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.5, 120.0, 1.5], "穿透之后拖动照常落盘")

    def test_drag_survives_a_broken_view2d(self):
        """view2d 中途不可用时不崩、不写脏数据，也不静默中断。"""
        op, _ = self._press(self._pixel(60.0, 1.0))
        self.view2d.broken = True
        self._move(op, (10.0, 10.0))
        self._release(op, (10.0, 10.0))
        self.assertIsNone(self.strip.get(PROP_KEY), "拿不到坐标就不该落盘")
        self.assertEqual(status.STATE["error"], "",
                         "不该报「编辑中断」：%r" % status.STATE["error"])

    def test_right_click_on_a_point_opens_the_delete_menu_path(self):
        """右键点上 → 存目标 + 弹菜单路径；随后删除要落盘三层。"""
        curve = carrier.STORE.get_or_create(self.strip)
        self.assertTrue(curve.insert_point(60.0, 1.0))
        carrier.STORE.commit(self.strip, curve)
        carrier.write_curve(self.strip, curve)
        self.assertEqual(len(self._native_keys()), 3)

        stub = _OperatorStub()
        # 注意必须显式调 point_menu 自己的 _start：_OperatorStub 类上绑的是
        # 拖动 operator 的同名方法，invoke(stub) 会经实例查找拿错 _start。
        result = interaction.SEQVEL_OT_point_menu._start(
            stub, self.context, _event("RIGHTMOUSE", mouse=self._pixel(60.0, 1.0)))
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(interaction._POINT_TARGET.strip_name, self.strip.name)
        self.assertEqual(interaction._POINT_TARGET.index, 1)

        deleter = _OperatorStub()
        result = interaction.SEQVEL_OT_delete_point.execute(deleter, self.context)
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([point for point in curve.points],
                         [(0.0, 1.0), (120.0, 1.0)], "删除要点消失")
        self.assertEqual(len(self._native_keys()), 2, "删除也要同步原生关键帧")

    def test_right_click_away_from_a_point_passes_through(self):
        """右键没按在点上：穿透（原生右键行为原样发生）。"""
        stub = _OperatorStub()
        # 显式调 point_menu 自己的 _start（替身类上绑的是拖动 operator 的同名方法）
        result = interaction.SEQVEL_OT_point_menu._start(
            stub, self.context, _event("RIGHTMOUSE",
                                       mouse=self._pixel(60.0, 2.5)))
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertIsNone(interaction._POINT_TARGET)

    def test_expired_point_target_refuses_to_delete(self):
        """目标过期（右键选了很久之后才直调删除）：绝不能删掉看不见的旧点。"""
        curve = carrier.STORE.get_or_create(self.strip)
        self.assertTrue(curve.insert_point(60.0, 1.0))
        carrier.STORE.commit(self.strip, curve)
        interaction._set_point_target(self.strip, 1, made_at=0.0)

        deleter = _OperatorStub()
        result = interaction.SEQVEL_OT_delete_point.execute(deleter, self.context)
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(len(curve.points), 3, "过期目标不删")

    def test_delete_point_keeps_two_points_and_protects_the_first(self):
        """保底 2 点 + 首点保护：拒绝要有信息，不能静默。"""
        curve = carrier.STORE.get_or_create(self.strip)
        interaction._set_point_target(self.strip, 0)
        deleter = _OperatorStub()
        result = interaction.SEQVEL_OT_delete_point.execute(deleter, self.context)
        self.assertEqual(result, {"CANCELLED"})
        self.assertTrue(deleter.reports, "首点拒绝要 report")
        self.assertEqual(len(curve.points), 2)

    # ------------------------------------------------- 右键段菜单（ADR-0005 R5）

    def _set_type(self, kind):
        stub = _OperatorStub()
        stub.kind = kind
        result = interaction.SEQVEL_OT_set_segment_type.execute(stub, self.context)
        return stub, result

    def test_right_click_on_a_segment_sets_its_type_and_writes_everything(self):
        """右键线上 → 段目标；选「平滑」→ 模型/属性/原生关键帧三层都变。"""
        curve = carrier.STORE.get_or_create(self.strip)
        # 端点速度必须**不同**：恒速的缓动段是常数，只需两端关键帧（ADR-0007 决定 6），
        # 那种形状验不出"逐帧落关键帧"。
        curve.insert_point(60.0, 1.8)
        carrier.STORE.commit(self.strip, curve)
        carrier.write_curve(self.strip, curve)

        stub = _OperatorStub()
        result = interaction.SEQVEL_OT_point_menu._start(
            stub, self.context,
            _event("RIGHTMOUSE",
                   mouse=self._pixel(30.0, curve.line_speed_at(30.0))))
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(interaction._SEGMENT_TARGET.strip_name, self.strip.name)
        self.assertEqual(interaction._SEGMENT_TARGET.index, 0)
        self.assertIsNone(interaction._POINT_TARGET, "按在线上不该记录点目标")

        _stub2, result = self._set_type("smooth")
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(curve.types, ["smooth", "linear"])
        self.assertEqual([int(v) for v in self.strip[store.TYPES_KEY]], [1, 0])
        # 缓动段逐帧落关键帧：只写端点的话 Blender 侧会把它拉回直线
        keys = self._native_keys()
        self.assertEqual(len(keys), len(curve.keyframe_frames()))
        self.assertTrue(len(keys) > len(curve.points))

    def test_right_click_on_a_segment_without_a_model_builds_one(self):
        """还没有曲线时右键线上也能选类型：顺手建模型（可见=可抓）。"""
        self.assertIsNone(carrier.STORE.get(self.strip))
        stub = _OperatorStub()
        result = interaction.SEQVEL_OT_point_menu._start(
            stub, self.context, _event("RIGHTMOUSE", mouse=self._pixel(60.0, 1.0)))
        self.assertEqual(result, {"FINISHED"})
        _stub2, result = self._set_type("slow")
        self.assertEqual(result, {"FINISHED"})
        curve = carrier.STORE.get(self.strip)
        self.assertIsNotNone(curve, "选类型应当把模型建起来")
        self.assertEqual(curve.types, ["slow"])
        self.assertIn(PROP_KEY, self.strip.keys(), "建了模型就要落盘")

    def test_setting_the_same_type_does_not_touch_anything(self):
        """选同一个类型 = 空操作：不留多余撤销步、不凭空生成数据。"""
        curve = carrier.STORE.get_or_create(self.strip)
        curve.insert_point(60.0, 1.0)
        interaction._set_segment_target(self.strip, 0)
        stub, result = self._set_type("linear")
        self.assertEqual(result, {"FINISHED"})
        self.assertTrue(stub.reports, "要说明「已经是线性」")
        self.assertNotIn(PROP_KEY, self.strip.keys(), "空操作不许写属性")
        self.assertEqual(curve.types, ["linear", "linear"])

    def test_right_click_on_the_tail_extension_passes_through(self):
        """末点右侧的延长线不是段：右键交还原生（本票的显式假设）。"""
        curve = carrier.STORE.get_or_create(self.strip)
        curve.move_point(1, rel_frame=40.0)
        carrier.STORE.commit(self.strip, curve)
        stub = _OperatorStub()
        result = interaction.SEQVEL_OT_point_menu._start(
            stub, self.context, _event("RIGHTMOUSE", mouse=self._pixel(90.0, 1.0)))
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertIsNone(interaction._SEGMENT_TARGET)

    def test_expired_segment_target_refuses_to_set_type(self):
        curve = carrier.STORE.get_or_create(self.strip)
        interaction._set_segment_target(self.strip, 0, made_at=0.0)
        _stub, result = self._set_type("fast")
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(curve.types, ["linear"], "过期目标不许改类型")

    def test_set_segment_type_without_a_target_passes_through(self):
        """没有右键目标（F3/脚本直调）：静默穿透，绝不凭空改别人的曲线。"""
        curve = carrier.STORE.get_or_create(self.strip)
        interaction._SEGMENT_TARGET = None
        _stub, result = self._set_type("smooth")
        self.assertEqual(result, {"PASS_THROUGH"})
        self.assertEqual(curve.types, ["linear"])


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestFineTuneDrag(unittest.TestCase):
    """Ctrl 微调拖动（ADR-0005 修订 R9）的全链路回归。

    与 TestDirectDrag 同一套"按下→MOUSEMOVE→松手"纪律，加一件事：事件带
    **Ctrl 状态**（事件级实时），断言三种拖动在微调档下的语义：
    增量减半、点拖帧冻结、翻转瞬间重锚零跳变。
    """

    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        status.clear_error()
        status.set_editing("")
        self.editor = bpy.context.scene.sequence_editor
        self.strip = make_strip(length=120)
        for strip in self.editor.strips:
            strip.select = False
        self.strip.select = True
        self.editor.active_strip = self.strip
        self.base = float(self.strip.left_handle)
        self.channel = float(self.strip.channel)
        self.view2d = FakeView2D()
        self.context, self.regions = make_editor_context(
            bpy.context.scene, self.view2d, active="WINDOW")

    def tearDown(self):
        status.clear_error()
        status.set_editing("")
        interaction._CLICKS.forget()

    def _pixel(self, rel_frame, speed):
        return geometry.point_xy(self.view2d, self.base, self.channel,
                                 rel_frame, speed)

    def _send(self, kind, value, mouse, ctrl=False):
        event = _event(kind, value=value, mouse=mouse)
        event.ctrl = ctrl
        return event

    def _press(self, mouse, ctrl=False):
        op = _OperatorStub()
        result = interaction.SEQVEL_OT_edit_speedline.invoke(
            op, self.context, self._send("LEFTMOUSE", "PRESS", mouse, ctrl))
        return op, result

    def _move(self, op, mouse, ctrl=False):
        return interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, self._send("MOUSEMOVE", "NOTHING", mouse, ctrl))

    def _release(self, op, mouse, ctrl=False):
        return interaction.SEQVEL_OT_edit_speedline.modal(
            op, self.context, self._send("LEFTMOUSE", "RELEASE", mouse, ctrl))

    def _middle_point(self):
        curve = carrier.STORE.get_or_create(self.strip)
        self.assertTrue(curve.insert_point(60.0, 1.0))
        carrier.STORE.commit(self.strip, curve)

    def test_ctrl_press_on_a_point_fine_tunes_half_speed_with_frozen_frame(self):
        """Ctrl 按住点上起拖：增量减半、帧冻结；松手三层落盘。"""
        self._middle_point()
        op, result = self._press(self._pixel(60.0, 1.0), ctrl=True)
        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(op.mode, "POINT")
        self.assertTrue(op.fine, "Ctrl 起拖应直接进入微调")
        self.assertIn("Fine", op._header_text(), "头部横幅要说明微调生效")

        # 光标挪到 (90 帧, 1.2x)：帧不动（不是 90），速度只挪一半 1.1
        self._move(op, self._pixel(90.0, 1.2), ctrl=True)
        self.assertEqual([round(f, 1) for f, _s in op.curve.points],
                         [0.0, 60.0, 120.0], "微调点拖的帧必须冻结")
        self.assertAlmostEqual(op.curve.points[1][1], 1.1, places=6)

        self._release(op, self._pixel(90.0, 1.2), ctrl=True)
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.0, 60.0, 1.1, 120.0, 1.0])
        keys = carrier.speed_curve(carrier.carrier_of(self.strip)).keyframe_points
        self.assertEqual([round(kp.co[1], 3) for kp in keys],
                         [1.0, 1.1, 1.0], "原生关键帧同步微调结果")

    def test_toggling_ctrl_mid_drag_reanchors_without_jump(self):
        """拖动中实时开关 Ctrl：翻转瞬间零跳变；退出后帧恢复跟随光标。"""
        self._middle_point()
        op, _ = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(90.0, 1.5))  # 1:1 绝对吸附 → 点=(90, 1.5)

        # 按住 Ctrl：重锚（快照=1.5、锚=当前光标速度 1.5）→ 一个数都不动
        self._move(op, self._pixel(70.0, 1.5), ctrl=True)
        self.assertEqual(op.curve.points[1][0], 90.0, "翻转瞬间帧不动")
        self.assertAlmostEqual(op.curve.points[1][1], 1.5, places=6,
                               msg="翻转瞬间零跳变（映射往返的浮点噪声内视为相等）")

        # 微调中：帧冻结在 90，速度只挪一半（1.5 → 1.6）
        self._move(op, self._pixel(80.0, 1.7), ctrl=True)
        self.assertEqual(op.curve.points[1][0], 90.0, "微调中帧冻结")
        self.assertAlmostEqual(op.curve.points[1][1], 1.6, places=6)

        # 松开 Ctrl：回到 1:1 绝对吸附，帧跳到光标当前帧（预期语义）
        self._move(op, self._pixel(80.0, 1.7))
        self.assertEqual(op.curve.points[1][0], 80.0)
        self.assertAlmostEqual(op.curve.points[1][1], 1.7, places=6)

        self._release(op, self._pixel(80.0, 1.7))
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.0, 80.0, 1.7, 120.0, 1.0])

    def test_ctrl_mid_segment_drag_halves_the_delta(self):
        """段拖中按住 Ctrl：重锚后增量减半；松开恢复 1:1。"""
        op, _ = self._press(self._pixel(60.0, 1.0))
        self._move(op, self._pixel(60.0, 1.5))  # 1:1 → 两端 1.5

        self._move(op, self._pixel(60.0, 1.9), ctrl=True)  # 翻转：重锚，无变化
        self.assertEqual([round(s, 3) for _f, s in op.curve.points],
                         [1.5, 1.5], "翻转瞬间零跳变")
        self._move(op, self._pixel(60.0, 2.1), ctrl=True)
        self.assertAlmostEqual(op.curve.points[0][1], 1.6, places=6,
                               msg="光标再挪 0.2x，端点只挪一半")
        self.assertAlmostEqual(op.curve.points[1][1], 1.6, places=6)

        # 松开 Ctrl：重锚在**本事件的光标位置**（2.3）→ 该事件内增量为 0
        # （零跳变）；光标继续挪才是 1:1 满速
        self._move(op, self._pixel(60.0, 2.3))
        self.assertAlmostEqual(op.curve.points[0][1], 1.6, places=6,
                               msg="切换瞬间零跳变")
        self._move(op, self._pixel(60.0, 2.5))
        self.assertAlmostEqual(op.curve.points[0][1], 1.8, places=6,
                               msg="回 1:1 后恢复满速")

        self._release(op, self._pixel(60.0, 2.5))
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.8, 120.0, 1.8])

    def test_ctrl_press_on_a_segment_is_fine_from_the_start(self):
        """[回归] Ctrl **起拖**的段拖必须直接进减半档，而不是等中途翻转才换。

        首版在按下时 fine=True 但会话仍以默认 scale=1.0 创建——Ctrl 起拖的
        整段拖动一直 1:1，规格"按下即微调"静默失效。
        """
        op, result = self._press(self._pixel(60.0, 1.0), ctrl=True)
        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(op.mode, "SEGMENT")
        self.assertTrue(op.fine)
        self.assertEqual(op.drag.scale, 0.5, "按下时的会话就得是减半档")

        self._move(op, self._pixel(60.0, 1.4), ctrl=True)  # 锚=线上速度 1.0
        self.assertAlmostEqual(op.curve.points[0][1], 1.2, places=6,
                               msg="起拖即减半：光标挪 0.4x，端点只挪 0.2x")
        self._release(op, self._pixel(60.0, 1.4), ctrl=True)
        self.assertEqual([round(float(v), 3) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.2, 120.0, 1.2])

    def test_without_ctrl_everything_stays_one_to_one(self):
        """不碰 Ctrl 时既有语义一个字不变（R9 只修饰增量，别无其他）。"""
        self._middle_point()  # 不插中间点的话按在 (60, 1.0) 上是段拖
        op, _ = self._press(self._pixel(60.0, 1.0))
        self.assertFalse(op.fine)
        self._move(op, self._pixel(90.0, 1.5))
        self.assertEqual(op.curve.points[1][0], 90.0)
        self.assertAlmostEqual(op.curve.points[1][1], 1.5, places=6)
        self._release(op, self._pixel(90.0, 1.5))
        self.assertEqual([round(float(v), 2) for v in self.strip.get(PROP_KEY)],
                         [0.0, 1.0, 90.0, 1.5, 120.0, 1.0])


if __name__ == "__main__":
    unittest.main()
