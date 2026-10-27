# -*- coding: utf-8 -*-
"""接缝 1（续）：条带坐标换算与命中检测的单测（纯 Python）。"""

import unittest

from seqvel import geometry
from seqvel.curve import SLOW, SPEED_MAX, SPEED_MIN, Curve

from support import FakeView2D, LegacyView2D


class TestCoordinateMapping(unittest.TestCase):
    def setUp(self):
        self.view = FakeView2D()
        self.curve = Curve.default(100)
        self.base = 1.0
        self.channel = 1.0

    def test_one_x_sits_at_one_third_of_the_ruler(self):
        _x, y = geometry.point_xy(self.view, self.base, self.channel, 0.0, 1.0)
        # 通道 1 的像素区间为 y ∈ [60, 120]；标尺跨通道高的 9%~95%（Blender 内缩
        # 5% + 底部留白 4%，ADR-0010 修订 R1）⇒ 1.0x 在 1/3 处 = 60 + 0.3767×60 ≈ 82.6
        self.assertAlmostEqual(y, 82.6)

    def test_speed_max_sits_on_the_strip_top_edge(self):
        _x, y = geometry.point_xy(self.view, self.base, self.channel, 0.0, SPEED_MAX)
        # 上限速度贴素材上缘：通道顶 120 收 5% → 60 + 0.95×60 = 117（ADR-0010）
        self.assertAlmostEqual(y, 117.0)

    def test_x_tracks_relative_frame(self):
        x0, _y = geometry.point_xy(self.view, self.base, self.channel, 0.0, 1.0)
        x1, _y = geometry.point_xy(self.view, self.base, self.channel, 30.0, 1.0)
        self.assertAlmostEqual(x1 - x0, 30.0 * self.view.x_scale)

    def test_mouse_round_trip(self):
        pixel = geometry.point_xy(self.view, self.base, self.channel, 42.0, 1.6)
        rel, speed = geometry.mouse_to_rel_speed(self.view, self.base, self.channel, pixel)
        self.assertAlmostEqual(rel, 42.0, places=6)
        self.assertAlmostEqual(speed, 1.6, places=6)

    def test_mouse_maps_into_speed_range(self):
        below = geometry.mouse_to_rel_speed(self.view, self.base, self.channel, (0.0, 0.0))
        above = geometry.mouse_to_rel_speed(self.view, self.base, self.channel, (0.0, 999.0))
        self.assertAlmostEqual(below[1], SPEED_MIN)
        self.assertAlmostEqual(above[1], SPEED_MAX)

    def test_unavailable_view2d_yields_none(self):
        broken = FakeView2D()
        broken.broken = True
        self.assertIsNone(geometry.point_xy(broken, self.base, self.channel, 0.0, 1.0))
        self.assertIsNone(geometry.curve_xy(broken, self.base, self.channel, self.curve.points))
        self.assertIsNone(geometry.mouse_to_rel_speed(broken, self.base, self.channel, (10.0, 10.0)))

    def test_view2d_is_asked_not_to_clip(self):
        geometry.point_xy(self.view, self.base, self.channel, 0.0, 1.0)
        self.assertEqual(self.view.clip_flags[-1], False)

    def test_two_argument_view2d_is_supported(self):
        # LegacyView2D：y = 通道 × 3.0；1.0x 在标尺（0.09~0.95，ADR-0010 修订 R1）
        # 1/3 处 ⇒ yfrac = 0.3767 ⇒ y ≈ 1.13
        self.assertAlmostEqual(geometry.point_xy(LegacyView2D(), 0.0, 0.0, 5.0, 1.0)[1], 1.13)
        self.assertEqual(geometry.point_xy(LegacyView2D(), 0.0, 0.0, 5.0, 1.0)[0], 10.0)

    def test_broken_view2d_object_never_raises(self):
        class Hostile(object):
            def view_to_region(self, *args):
                raise RuntimeError("boom")

            def region_to_view(self, *args):
                raise RuntimeError("boom")

        self.assertIsNone(geometry.point_xy(Hostile(), 0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(geometry.region_to_view(Hostile(), 0.0, 0.0))


class TestHitTest(unittest.TestCase):
    def setUp(self):
        self.view = FakeView2D()
        self.base = 1.0
        self.channel = 1.0
        self.duration = 100.0
        self.curve = Curve([(0.0, 1.0), (50.0, 1.0), (100.0, 1.0)], self.duration)

    def hit(self, frame, speed):
        mouse = geometry.point_xy(self.view, self.base, self.channel, frame, speed)
        return geometry.hit_test(self.view, self.base, self.channel, self.duration,
                                 self.curve, mouse)

    def test_hits_point_first(self):
        result = self.hit(50.0, 1.0)
        self.assertEqual(result.kind, geometry.HIT_POINT)
        self.assertEqual(result.index, 1)

    def test_point_wins_over_segment_on_overlap(self):
        # 鼠标落在中点附近：既贴点也在段上，必须优先判为点
        mouse = geometry.point_xy(self.view, self.base, self.channel, 50.0, 1.0)
        mouse = (mouse[0] + 6.0, mouse[1])
        result = geometry.hit_test(self.view, self.base, self.channel, self.duration,
                                   self.curve, mouse)
        self.assertEqual(result.kind, geometry.HIT_POINT)

    def test_hits_segment_between_points(self):
        result = self.hit(25.0, 1.0)
        self.assertEqual(result.kind, geometry.HIT_SEGMENT)
        self.assertEqual(result.index, 0)

    # ------------------------------------------------------ 段命中取最近（票 05）

    def _sharp_corner(self):
        """尖 V（段内角约 67°）+ 1 像素/帧的视口：拐点附近两段会同时进 8px 半径。

        **必须是全线性**：线性段的采样折线 == 模型点折线，因此"哪一段更近"可以
        直接拿模型点算出来当基准。缓动段不成立（采样会掰弯折线）。
        """
        view = FakeView2D(x_scale=1.0)
        curve = Curve([(0.0, 3.0), (10.0, 0.1), (100.0, 3.0)], self.duration)
        return view, curve

    def test_sharp_corner_picks_the_nearer_segment(self):
        """两段同时进半径时，抓到的是**更近**的那段，不是**先遍历到**的那段。

        回归（票 05）：段循环原先写的是"第一个进半径就 return"，从下标 0 起遍历，
        于是永远偏向下标小的段、与鼠标距离无关。症状是右键改错段的形状，
        更糟的是拖错段（会改数据）。
        """
        view, curve = self._sharp_corner()
        pixels = geometry.curve_xy(view, self.base, self.channel, curve.points)
        margin = geometry.POINT_RADIUS + geometry.LINE_RADIUS
        corner_x, corner_y = pixels[1]
        ambiguous = 0
        for x in range(int(corner_x - margin), int(corner_x + margin) + 1):
            for y in range(int(corner_y - margin), int(corner_y + margin) + 1):
                mouse = (float(x), float(y))
                if min(geometry.dist(x, y, px, py) for px, py in pixels) < geometry.POINT_RADIUS:
                    continue  # 会先判成点命中，这条用例不管
                distances = [geometry.dist_point_segment(x, y, a[0], a[1], b[0], b[1])
                             for a, b in zip(pixels, pixels[1:])]
                if not [d for d in distances if d < geometry.LINE_RADIUS]:
                    continue  # 半径外
                if len([d for d in distances if d < geometry.LINE_RADIUS]) > 1:
                    ambiguous += 1
                hit = geometry.hit_test(view, self.base, self.channel, self.duration,
                                        curve, mouse)
                nearest = min(range(len(distances)), key=lambda i: distances[i])
                self.assertEqual(hit.kind, geometry.HIT_SEGMENT, mouse)
                self.assertEqual(hit.index, nearest, mouse)
        self.assertGreater(ambiguous, 0, "这个构造必须真的产生歧义像素，否则用例是空的")

    def test_corner_tie_breaks_to_the_lower_index(self):
        """两段**逐位等距**时取下标较小者——结果稳定可预测，不看遍历以外的运气。

        构造：左右对称的尖 V，鼠标放在对称轴上。用例自己断言两段距离**逐位相等**，
        免得哪天构造被改坏了、退化成一条"其实没在测平手"的空用例。
        """
        view = FakeView2D(x_scale=1.0)
        duration = 50.0
        curve = Curve([(0.0, 3.0), (25.0, 0.1), (50.0, 3.0)], duration)
        pixels = geometry.curve_xy(view, self.base, self.channel, curve.points)
        corner_x, corner_y = pixels[1]
        # 13.5 而不是 13：逐位等距是浮点上的"恰好"，标尺改成 0.09~0.95 后
        # 13.0 的两段距离差 3e-15（不再是真·平手），13.5 才是精确等距的一档。
        mouse = (corner_x, corner_y + 13.5)
        distances = [geometry.dist_point_segment(mouse[0], mouse[1], a[0], a[1], b[0], b[1])
                     for a, b in zip(pixels, pixels[1:])]
        self.assertEqual(distances[0], distances[1], "构造必须是真·等距，否则没在测平手")
        self.assertLess(distances[0], geometry.LINE_RADIUS)
        hit = geometry.hit_test(view, self.base, self.channel, duration, curve, mouse)
        self.assertEqual(hit.kind, geometry.HIT_SEGMENT)
        self.assertEqual(hit.index, 0)

    def test_eased_segment_is_grabbable_where_it_is_drawn(self):
        """缓动段：命中跟着**画出来的弯线**走，不再命中两点之间的直线弦。"""
        curve = Curve([(0.0, 1.0), (100.0, 3.0)], self.duration, types=[SLOW])
        on_curve = geometry.point_xy(self.view, self.base, self.channel, 50.0, 1.5)
        on_chord = geometry.point_xy(self.view, self.base, self.channel, 50.0, 2.0)
        hit = geometry.hit_test(self.view, self.base, self.channel, self.duration, curve, on_curve)
        self.assertEqual(hit.kind, geometry.HIT_SEGMENT)
        self.assertEqual(hit.index, 0)  # 段下标仍是**模型段号**（拖动按模型段来）
        missed = geometry.hit_test(self.view, self.base, self.channel, self.duration, curve, on_chord)
        self.assertEqual(missed.kind, geometry.HIT_NONE)

    def test_sample_step_follows_zoom(self):
        """采样步长按视口横向比例折算（约 2 像素）：放大有下限保护、缩小自然变粗。"""
        self.assertAlmostEqual(geometry.sample_step(FakeView2D(x_scale=10.0)), geometry.MIN_SAMPLE_STEP)
        self.assertAlmostEqual(geometry.sample_step(FakeView2D(x_scale=1.0)), 2.0)
        self.assertAlmostEqual(geometry.sample_step(FakeView2D(x_scale=0.01)), 200.0)
        self.assertEqual(geometry.sample_step(None), 1.0)

    def test_hits_tail_extension_when_last_point_moved_left(self):
        curve = Curve.default(self.duration)
        curve.move_point(1, rel_frame=40.0)
        mouse = geometry.point_xy(self.view, self.base, self.channel, 80.0, 1.0)
        result = geometry.hit_test(self.view, self.base, self.channel, self.duration, curve, mouse)
        self.assertEqual(result.kind, geometry.HIT_TAIL)

    def test_no_tail_hit_when_last_point_at_end(self):
        mouse = geometry.point_xy(self.view, self.base, self.channel, 100.0, 1.0)
        mouse = (mouse[0] + 30.0, mouse[1])
        result = geometry.hit_test(self.view, self.base, self.channel, self.duration,
                                   self.curve, mouse)
        self.assertEqual(result.kind, geometry.HIT_NONE)

    def test_empty_space_is_no_hit(self):
        result = self.hit(50.0, 1.8)
        self.assertEqual(result.kind, geometry.HIT_NONE)

    def test_segment_slope_is_tracked(self):
        curve = Curve([(0.0, 1.0), (100.0, 2.0)], self.duration)
        mouse = geometry.point_xy(self.view, self.base, self.channel, 70.0, 1.7)
        result = geometry.hit_test(self.view, self.base, self.channel, self.duration, curve, mouse)
        self.assertEqual(result.kind, geometry.HIT_SEGMENT)

    def test_offscreen_curve_does_not_match_everything(self):
        """视口外的点不再让整条线"消失"：换算得到坐标，但不会误命中。"""
        far_view = FakeView2D(frame_origin=1000.0)
        mouse = (10.0, 100.0)
        result = geometry.hit_test(far_view, self.base, self.channel, self.duration,
                                   self.curve, mouse)
        self.assertEqual(result.kind, geometry.HIT_NONE)

    def test_baseline_and_tail_helpers(self):
        baseline = geometry.baseline_xy(self.view, self.base, self.channel, self.duration)
        self.assertEqual(len(baseline), 2)
        _x0, y0 = baseline[0]
        _x1, y1 = baseline[1]
        self.assertAlmostEqual(y0, y1)
        self.assertNotIn(None, geometry.tail_end_xy(self.view, self.base, self.channel, 0.0, 1.0))

    def test_speed_range_label(self):
        self.assertEqual(geometry.speed_range_label(), "0 ~ 3.0x")


class TestView2DMask(unittest.TestCase):
    """view2d_mask：GPU 裁剪矩形（网格区）的探测计算（纯 Python）。

    FakeView2D 的 clip_rect 即"网格 widget 矩形"：clip=True 的换算在点
    落到矩形外时返回 None，与 5.2.1 真机行为一致。
    """

    WIDTH, HEIGHT = 640, 480
    MASK = (0.0, 0.0, 600.0, 420.0)  # 区域右/下各留出滚动条

    def masked_view(self, **kwargs):
        return FakeView2D(clip_rect=self.MASK, **kwargs)

    def assert_mask(self, mask):
        self.assertIsNotNone(mask)
        for value, expected in zip(mask, self.MASK):
            self.assertAlmostEqual(value, expected, places=4)

    def test_mask_matches_widget_rect(self):
        self.assert_mask(geometry.view2d_mask(self.masked_view(), self.WIDTH, self.HEIGHT))

    def test_mask_ignores_zoom_and_pan(self):
        """缩放/横移只改视图范围，mask 是区域固定的——遮蔽必须与视图状态无关。"""
        near = self.masked_view()
        far = self.masked_view(frame_origin=5000.0, x_scale=0.05, y_scale=2.0)
        for value, expected in zip(geometry.view2d_mask(near, self.WIDTH, self.HEIGHT),
                                   geometry.view2d_mask(far, self.WIDTH, self.HEIGHT)):
            self.assertAlmostEqual(value, expected, places=4)

    def test_mask_clamped_to_region(self):
        """探测结果超出区域矩形时被钳制：mask 永远 ⊆ 区域（兜底约定）。"""
        view = FakeView2D(clip_rect=(0.0, 0.0, 900.0, 700.0))
        mask = geometry.view2d_mask(view, self.WIDTH, self.HEIGHT)
        self.assertIsNotNone(mask)
        for value, expected in zip(mask, (0.0, 0.0, 640.0, 480.0)):
            self.assertAlmostEqual(value, expected, places=4)

    def test_probe_uses_clip_mode(self):
        view = self.masked_view()
        geometry.view2d_mask(view, self.WIDTH, self.HEIGHT)
        self.assertIn(True, view.clip_flags)

    def test_view2d_without_clip_support_yields_none(self):
        """只有旧双参签名（无法探测）→ None，绘制方退回区域矩形兜底。"""
        self.assertIsNone(geometry.view2d_mask(LegacyView2D(), self.WIDTH, self.HEIGHT))

    def test_view2d_that_ignores_clip_clamps_to_region(self):
        """clip 参数被无视（永远有值）→ 探测退化为区域矩形，行为可预期。"""
        view = FakeView2D()  # clip_rect=None：clip=True 照常返回坐标
        mask = geometry.view2d_mask(view, self.WIDTH, self.HEIGHT)
        self.assertIsNotNone(mask)
        for value, expected in zip(mask, (0.0, 0.0, 640.0, 480.0)):
            self.assertAlmostEqual(value, expected, places=4)

    def test_broken_view2d_yields_none(self):
        broken = FakeView2D(clip_rect=self.MASK)
        broken.broken = True
        self.assertIsNone(geometry.view2d_mask(broken, self.WIDTH, self.HEIGHT))

    def test_hostile_view2d_yields_none(self):
        class Hostile(object):
            def view_to_region(self, *args):
                raise RuntimeError("boom")

            def region_to_view(self, *args):
                raise RuntimeError("boom")

        self.assertIsNone(geometry.view2d_mask(Hostile(), self.WIDTH, self.HEIGHT))

    def test_nan_view2d_yields_none(self):
        class DeadView2D(object):
            def view_to_region(self, frame, channel, clip=True):
                return (float("nan"), float("nan"))

            def region_to_view(self, x, y):
                return (float("nan"), float("nan"))

        self.assertIsNone(geometry.view2d_mask(DeadView2D(), self.WIDTH, self.HEIGHT))

    def test_degenerate_mask_yields_none(self):
        # 中心点(320,240)不在零宽矩形内 → 视口内无可用探测起点
        view = FakeView2D(clip_rect=(100.0, 100.0, 100.0, 300.0))
        self.assertIsNone(geometry.view2d_mask(view, self.WIDTH, self.HEIGHT))

    def test_invalid_region_size_yields_none(self):
        view = self.masked_view()
        self.assertIsNone(geometry.view2d_mask(view, 0, self.HEIGHT))
        self.assertIsNone(geometry.view2d_mask(view, self.WIDTH, None))


def _sentinel_view(mask=(0.0, 0.0, 600.0, 420.0)):
    """5.2.1 真机语义的 view2d 假对象（见 TestSentinelView2D 类文档）。"""
    x0, y0, x1, y1 = mask

    class SentinelView2D(object):
        def view_to_region(self, frame, channel, *, clip=True):
            # 像素 = 帧/通道线性映射（1 帧 10 px、1 通道 60 px）
            x = float(frame) * 10.0
            y = float(channel) * 60.0
            if clip and not (x0 <= x <= x1 and y0 <= y <= y1):
                return (12000.0, 12000.0)  # 真机哨兵
            return (x, y)

        def region_to_view(self, x, y):
            return (float(x) / 10.0, float(y) / 60.0)

    return SentinelView2D()


class TestSentinelView2D(unittest.TestCase):
    """模拟 5.2.1 真机语义的 view2d。

    真机实测（GUI 探针，1358x862 时间轴区域）：
    - ``clip`` 是 RNA 关键字参数，位置传参直接 TypeError；
    - clip=False / clip=True 都不返回 None；出视口时 clip=True 返回
      哨兵值 (12000, 12000)；
    - 历史 bug 链：位置传参失败 → 静默退回 2 参（等效 clip=True）→
      出界曲线段全被画向 (12000, 12000) 那个远处固定点。
    """

    WIDTH, HEIGHT = 640, 480
    MASK = (0.0, 0.0, 600.0, 420.0)

    def test_keyword_clip_false_gives_unclipped_coordinates(self):
        # 出视口的点：clip=False 必须给真实坐标，而不是哨兵
        xy = geometry.view_to_region(_sentinel_view(), 100.0, 1.0)
        self.assertAlmostEqual(xy[0], 1000.0)
        self.assertAlmostEqual(xy[1], 60.0)

    def test_mask_probed_through_sentinel_semantics(self):
        mask = geometry.view2d_mask(_sentinel_view(), self.WIDTH, self.HEIGHT)
        self.assertIsNotNone(mask)
        for value, expected in zip(mask, self.MASK):
            self.assertAlmostEqual(value, expected, places=4)


class TestViewToRegionClipped(unittest.TestCase):
    """_view_to_region_clipped：探测缝合线的直接单测。

    判界方式是"clip=True 与不裁剪换算的结果对比"，因此对三种上游实现
    都能正确判出视口：返回 None（旧实现）、返回哨兵（5.2.1）、钳到边界。
    """

    def test_inside_point_passes_through(self):
        view = FakeView2D(frame_origin=0.0, clip_rect=(0.0, 0.0, 600.0, 420.0))
        xy = geometry._view_to_region_clipped(view, 30.0, 5.0)
        self.assertAlmostEqual(xy[0], 300.0)
        self.assertAlmostEqual(xy[1], 300.0)

    def test_sentinel_outside_is_detected(self):
        # 5.2.1：出视口 clip=True 返回 (12000, 12000) 哨兵
        self.assertIsNone(geometry._view_to_region_clipped(_sentinel_view(), 100.0, 1.0))

    def test_none_outside_is_detected(self):
        # 旧实现：出视口 clip=True 直接返回 None
        view = FakeView2D(clip_rect=(0.0, 0.0, 600.0, 420.0))
        self.assertIsNone(geometry._view_to_region_clipped(view, 70.0, 5.0))

    def test_clamped_outside_is_detected(self):
        # 假设中的另一种实现：出视口钳到 mask 边缘（≠真实坐标 → 判出视口）
        class ClampView(object):
            def view_to_region(self, frame, channel, *, clip=True):
                x, y = float(frame) * 10.0, float(channel) * 60.0
                if clip:
                    return (min(x, 600.0), min(y, 420.0))
                return (x, y)

            def region_to_view(self, x, y):
                return (float(x) / 10.0, float(y) / 60.0)

        self.assertIsNone(geometry._view_to_region_clipped(ClampView(), 70.0, 5.0))

    def test_keyword_unsupported_yields_none(self):
        """只有旧双参签名（clip 关键字不接受）→ 无法探测，返回 None。"""
        self.assertIsNone(geometry._view_to_region_clipped(LegacyView2D(), 1.0, 1.0))

    def test_nan_yields_none(self):
        class DeadView(object):
            def view_to_region(self, frame, channel, *, clip=True):
                return (float("nan"), float("nan"))

            def region_to_view(self, x, y):
                return (float("nan"), float("nan"))

        self.assertIsNone(geometry._view_to_region_clipped(DeadView(), 1.0, 1.0))


class TestPickHit(unittest.TestCase):
    """跨条带择近（ADR-0005）：同屏多条速度线重叠时，按"点 > 线、近 > 远"挑。"""

    def setUp(self):
        self.view = FakeView2D()
        self.base = 1.0
        self.channel = 1.0

    def _hit(self, rel_frame, speed):
        """在给定模型坐标处做一次命中检测，返回 (hit, 鼠标像素)。"""
        mouse = geometry.point_xy(self.view, self.base, self.channel,
                                  rel_frame, speed)
        curve = Curve.default(100)
        hit = geometry.hit_test(self.view, self.base, self.channel,
                                100.0, curve, mouse)
        return hit, mouse

    def test_hit_distance_is_populated(self):
        """hit_test 现在要带像素距离（择近的依据）；NONE 则为 None。"""
        hit, _mouse = self._hit(60.0, 1.0)
        self.assertEqual(hit.kind, geometry.HIT_SEGMENT)
        self.assertIsNotNone(hit.distance)
        self.assertGreaterEqual(hit.distance, 0.0)

        none_hit = geometry.hit_test(self.view, self.base, self.channel,
                                     100.0, Curve.default(100), (0.0, 0.0))
        self.assertEqual(none_hit.kind, geometry.HIT_NONE)
        self.assertIsNone(none_hit.distance)

    def test_hit_distance_reflects_actual_proximity(self):
        """距离应是真实像素距离：离点 5 像素，距离就是 5。"""
        curve = Curve.default(100)
        point_px = geometry.point_xy(self.view, self.base, self.channel, 0.0, 1.0)
        near = geometry.hit_test(self.view, self.base, self.channel,
                                 100.0, curve, (point_px[0] + 5.0, point_px[1]))
        self.assertEqual(near.kind, geometry.HIT_POINT)
        self.assertAlmostEqual(near.distance, 5.0, places=3)

    def test_two_arg_hit_construction_still_works(self):
        """Hit 加距离字段后，旧的二参构造必须兼容（各处兼容性护栏）。"""
        hit = geometry.Hit(geometry.HIT_NONE, None)
        self.assertEqual(hit.kind, geometry.HIT_NONE)
        self.assertIsNone(hit.distance)

    def test_pick_hit_prefers_points_over_lines(self):
        """一条线的点与另一条线的段同屏：点赢，哪怕段更近。"""
        strip_line = object()
        strip_point = object()
        point_hit, _ = self._hit(60.0, 1.0)  # 命中 1.0x 基线中点 = SEGMENT
        # 构造：一条线上的点（距离 20）、另一条线上的段（距离 3）
        candidate_point = (strip_point, geometry.Hit(geometry.HIT_POINT, 0, 20.0))
        candidate_segment = (strip_line, geometry.Hit(geometry.HIT_SEGMENT, 0, 3.0))
        strip, hit = geometry.pick_hit([candidate_segment, candidate_point])
        self.assertIs(strip, strip_point)
        self.assertEqual(hit.kind, geometry.HIT_POINT)

    def test_pick_hit_prefers_nearest_within_same_kind(self):
        """同类命中（两条线的段）：取像素更近的。"""
        near_strip, far_strip = object(), object()
        candidates = [
            (far_strip, geometry.Hit(geometry.HIT_SEGMENT, 0, 12.0)),
            (near_strip, geometry.Hit(geometry.HIT_TAIL, None, 4.0)),
        ]
        strip, hit = geometry.pick_hit(candidates)
        self.assertIs(strip, near_strip)
        self.assertEqual(hit.kind, geometry.HIT_TAIL)

    def test_pick_hit_skips_none_and_handles_empty(self):
        strip = object()
        none_strip = object()
        got = geometry.pick_hit([
            (none_strip, geometry.Hit(geometry.HIT_NONE, None, None)),
            (strip, geometry.Hit(geometry.HIT_SEGMENT, 1, 9.0)),
        ])
        self.assertIs(got[0], strip)
        self.assertEqual(got[1].kind, geometry.HIT_SEGMENT)
        self.assertEqual(geometry.pick_hit([]), (None, None))
        self.assertEqual(
            geometry.pick_hit([(none_strip, geometry.Hit(geometry.HIT_NONE, None, None))]),
            (None, None))


if __name__ == "__main__":
    unittest.main()
