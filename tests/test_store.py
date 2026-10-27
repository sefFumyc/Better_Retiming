# -*- coding: utf-8 -*-
"""影子模型的缓存与持久化单测（headless Blender）。"""

import os
import tempfile
import unittest

from seqvel.curve import Curve

from support import HAVE_BPY, make_strip, reset_scene


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestModelStore(unittest.TestCase):
    def setUp(self):
        import bpy

        from seqvel import store

        reset_scene()
        store.STORE.invalidate()
        self.bpy = bpy
        self.store = store
        self.strip = make_strip(length=120)

    def test_no_model_before_first_edit(self):
        self.assertIsNone(self.store.STORE.get(self.strip))
        self.assertFalse(self.store.STORE.has_model(self.strip))

    def test_default_curve_is_not_persisted(self):
        curve = self.store.STORE.get_or_create(self.strip)
        self.assertEqual(curve.points, [(0.0, 1.0), (120.0, 1.0)])
        self.assertNotIn(self.store.PROP_KEY, self.strip.keys())

    def test_display_curve_always_available(self):
        displayed = self.store.STORE.display_curve(self.strip)
        self.assertEqual(displayed.points, [(0.0, 1.0), (120.0, 1.0)])
        self.assertIsNone(self.store.STORE.get(self.strip))  # 绘制不产生模型

    def test_commit_persists_flat_float_list(self):
        curve = self.store.STORE.get_or_create(self.strip)
        curve.insert_point(40.0, 1.5)
        self.assertTrue(self.store.STORE.commit(self.strip, curve))
        self.assertEqual([float(v) for v in self.strip[self.store.PROP_KEY]],
                         [0.0, 1.0, 40.0, 1.5, 120.0, 1.0])

    def test_cache_reload_from_property(self):
        curve = self.store.STORE.get_or_create(self.strip)
        curve.move_point(1, rel_frame=60.0, speed=1.25)
        self.store.STORE.commit(self.strip, curve)
        self.store.STORE.invalidate()
        restored = self.store.STORE.get(self.strip)
        self.assertEqual(restored.points, curve.points)

    def test_forget_drops_cache_and_property(self):
        curve = self.store.STORE.get_or_create(self.strip)
        self.store.STORE.commit(self.strip, curve)
        self.store.STORE.forget(self.strip)
        self.assertNotIn(self.store.PROP_KEY, self.strip.keys())
        self.assertNotIn(self.store.TYPES_KEY, self.strip.keys())
        self.assertIsNone(self.store.STORE.get(self.strip))

    def test_segment_types_survive_commit_and_reload(self):
        curve = self.store.STORE.get_or_create(self.strip)
        curve.insert_point(40.0)
        curve.set_segment_type(0, "smooth")
        curve.set_segment_type(1, "slow")
        self.assertTrue(self.store.STORE.commit(self.strip, curve))
        self.assertEqual([int(v) for v in self.strip[self.store.TYPES_KEY]], [1, 3])
        self.store.STORE.invalidate()
        restored = self.store.STORE.get(self.strip)
        self.assertEqual(restored.types, ["smooth", "slow"])
        self.assertAlmostEqual(restored.line_speed_at(20.0), curve.line_speed_at(20.0))

    def test_legacy_strip_without_types_reads_all_linear(self):
        """旧 .blend：只有点、没有类型属性 → 全部按线性读（不改点值）。"""
        self.strip[self.store.PROP_KEY] = [0.0, 1.0, 40.0, 1.5, 120.0, 1.0]
        curve = self.store.STORE.get(self.strip)
        self.assertEqual(curve.types, ["linear", "linear"])
        self.assertEqual(curve.points, [(0.0, 1.0), (40.0, 1.5), (120.0, 1.0)])

    def test_corrupt_payload_falls_back_to_default(self):
        self.strip[self.store.PROP_KEY] = [1.0]
        curve = self.store.STORE.get_or_create(self.strip)
        self.assertEqual(curve.points, [(0.0, 1.0), (120.0, 1.0)])

    def test_duration_follows_strip_resize(self):
        curve = self.store.STORE.get_or_create(self.strip)
        self.strip.right_handle = self.strip.left_handle + 60
        self.assertEqual(self.store.STORE.get(self.strip).duration, 60.0)
        self.assertIs(curve, self.store.STORE.get(self.strip))

    def test_key_is_scene_plus_strip_name(self):
        self.assertEqual(self.store.strip_key(self.strip),
                         (self.bpy.context.scene.name, self.strip.name))

    def test_strip_geometry_helpers(self):
        self.assertEqual(self.store.strip_base_frame(self.strip), float(self.strip.left_handle))
        self.assertEqual(self.store.strip_duration(self.strip), 120.0)
        self.assertEqual(self.store.strip_channel(self.strip), float(self.strip.channel))

    def test_curve_survives_save_and_reload(self):
        curve = self.store.STORE.get_or_create(self.strip)
        curve.insert_point(50.0, 0.4)
        self.store.STORE.commit(self.strip, curve)
        path = os.path.join(tempfile.gettempdir(), "seqvel_tests", "store_roundtrip.blend")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.bpy.ops.wm.save_as_mainfile(filepath=path)
        self.bpy.ops.wm.open_mainfile(filepath=path)
        editor = self.bpy.context.scene.sequence_editor
        self.assertIsNotNone(editor)
        strip = editor.strips[0]
        # 必须显式清缓存：缓存键是（场景名 + 条带名），重开文件后**一模一样**，
        # 不清就会拿到保存前那份内存曲线，"重载"根本没被验证。
        self.store.STORE.invalidate()
        restored = self.store.STORE.get(strip)
        self.assertIsNotNone(restored, "重开文件后应能从条带属性恢复曲线")
        self.assertAlmostEqual(restored.line_speed_at(50.0), 0.4, places=6)

    def test_legacy_blend_from_two_x_era_reads_back_unchanged(self):
        """[票 01 验收] 改上限**之前**存过曲线的 .blend，读出后逐点一致。

        旧文件的两条特征：载荷只有点、没有类型属性；值域按当时的 0~2.0 上限写就。
        抬高上限只放宽钳制范围，**不得**做归一化 / 重标定——否则用户旧工程里
        "2.0x 的那一段"会无声变成别的速度。
        """
        legacy = [0.0, 1.0, 40.0, 2.0, 90.0, 0.5, 120.0, 1.0]
        self.strip[self.store.PROP_KEY] = list(legacy)
        self.assertNotIn(self.store.TYPES_KEY, self.strip.keys())

        path = os.path.join(tempfile.gettempdir(), "seqvel_tests", "legacy_two_x_curve.blend")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.bpy.ops.wm.save_as_mainfile(filepath=path)
        self.bpy.ops.wm.open_mainfile(filepath=path)

        editor = self.bpy.context.scene.sequence_editor
        strip = next(s for s in editor.strips if s.name == self.strip.name)
        self.store.STORE.invalidate()

        # 1) 落盘载荷逐字节级一致：没被重写、没被缩放
        self.assertEqual([float(v) for v in strip[self.store.PROP_KEY]], legacy)
        # 2) 读回模型逐点一致（旧上限 2.0 恰好是新上限的 1/3，位置与值都不许动）
        curve = self.store.STORE.get(strip)
        self.assertIsNotNone(curve)
        self.assertEqual(curve.points, [(0.0, 1.0), (40.0, 2.0), (90.0, 0.5), (120.0, 1.0)])
        # 3) 没有类型属性 = 全线性（向后兼容），且模型自检通过
        self.assertEqual(curve.types, ["linear", "linear", "linear"])
        self.assertTrue(curve.is_valid())
        # 4) 旧值不被新上限重新钳制（2.0 < 3.0 原样保留，不是"等比放大到 3.0"）
        self.assertAlmostEqual(curve.line_speed_at(40.0), 2.0, places=6)

    def test_handlers_invalidate_cache_on_undo_and_load(self):
        handlers = self.bpy.app.handlers
        self.store.register_handlers()
        try:
            for name in self.store._HANDLER_NAMES:
                self.assertIn(self.store._invalidate_cache, getattr(handlers, name))
            curve = self.store.STORE.get_or_create(self.strip)
            self.store.STORE.commit(self.strip, curve)
            self.assertIsNotNone(self.store.STORE.get(self.strip))
            handlers.undo_post[0]()
            self.assertEqual(self.store.STORE._cache, {})
        finally:
            self.store.unregister_handlers()
        for name in self.store._HANDLER_NAMES:
            self.assertNotIn(self.store._invalidate_cache, getattr(handlers, name))


class _FakeSettings(object):
    """场景设置替身：带不带 ``default_curve_type`` 两种都要能表达。"""

    def __init__(self, kind=None):
        if kind is not None:
            self.default_curve_type = kind


class _FakeScene(object):
    def __init__(self, name="scene", kind=None):
        self.name = name
        if kind is not None:
            self.seqvel = _FakeSettings(kind)
        else:
            self.seqvel = None  # 旧场景 / 纯 Python 替身：没有设置


class _FakeStrip(object):
    """零 bpy 的条带替身：store 只用到 几何 + id_data + 类属性表。

    条带名必须逐条唯一：缓存键是（场景名 + 条带名），撞名会拿到上一条的曲线。
    """

    def __init__(self, name, scene=None, length=120.0, props=None):
        self.id_data = _FakeScene() if scene is None else scene
        self.name = name
        self.channel = 1
        self.left_handle = 1.0
        self.right_handle = 1.0 + float(length)
        self._props = dict(props or {})

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __getitem__(self, key):
        return self._props[key]

    def keys(self):
        return list(self._props)

    def __contains__(self, key):
        return key in self._props

    def __setitem__(self, key, value):
        self._props[key] = value

    def __delitem__(self, key):
        del self._props[key]


class TestDefaultSegmentType(unittest.TestCase):
    """[ADR-0007] 默认段曲线类型的读取路径与作用范围（零 bpy，替身驱动）。

    决定全文见 `docs/adr/0007-default-segment-type-read-once.md`：默认只在曲线
    **诞生**那一刻读一次，**不改**拆段继承 / 并段取左，也**不做**兜底值。
    """

    def setUp(self):
        from seqvel import store

        self.store = store
        self.store.STORE.invalidate()

    def tearDown(self):
        self.store.STORE.invalidate()

    # ---------- 读取路径 ----------

    def test_reads_the_strip_scene_setting(self):
        strip = _FakeStrip("read", scene=_FakeScene(kind="smooth"))
        self.assertEqual(self.store.default_segment_type(strip), "smooth")

    def test_falls_back_to_linear_when_nothing_to_read(self):
        """旧场景 / 属性缺失 / 垃圾值 / 没有 id_data / 连条带都不是 → 一律线性（绝不抛）。"""
        no_seqvel = _FakeStrip("fb0", scene=_FakeScene())
        empty_settings = _FakeStrip("fb1", scene=_FakeScene())
        empty_settings.id_data.seqvel = _FakeSettings()  # 有设置对象、没这个属性
        cases = {
            "旧场景（没有 seqvel 设置）": no_seqvel,
            "设置了对象但没有该属性": empty_settings,
            "垃圾字符串": _FakeStrip("fb2", scene=_FakeScene(kind="bogus")),
            "不认识的整数码": _FakeStrip("fb3", scene=_FakeScene(kind=99)),
        }
        for label, strip in cases.items():
            self.assertEqual(self.store.default_segment_type(strip), "linear", msg=label)
        orphan = _FakeStrip("fb4")
        del orphan.id_data
        self.assertEqual(self.store.default_segment_type(orphan), "linear")
        self.assertEqual(self.store.default_segment_type(None), "linear")

    def test_known_short_codes_are_accepted(self):
        """持久化短码（`TYPE_CODES`）也认——两套编码是同源映射，不做第二套。"""
        for code, kind in ((0, "linear"), (1, "smooth"), (2, "fast"), (3, "slow")):
            strip = _FakeStrip("code%d" % code, scene=_FakeScene(kind=code))
            self.assertEqual(self.store.default_segment_type(strip), kind)

    # ---------- 诞生时生效 ----------

    def test_new_curve_takes_the_scene_default(self):
        curve = self.store.STORE.get_or_create(
            _FakeStrip("new", scene=_FakeScene(kind="smooth")))
        self.assertEqual(curve.types, ["smooth"])
        self.assertEqual(curve.points, [(0.0, 1.0), (120.0, 1.0)])
        displayed = self.store.STORE.display_curve(
            _FakeStrip("disp", scene=_FakeScene(kind="fast")))
        self.assertEqual(displayed.types, ["fast"])

    def test_new_curve_without_settings_is_linear(self):
        curve = self.store.STORE.get_or_create(_FakeStrip("plain"))
        self.assertEqual(curve.types, ["linear"])

    def test_corrupt_payload_repair_uses_the_scene_default(self):
        """损坏载荷的修复路径也算"诞生"（ADR-0007 决定 1）。"""
        strip = _FakeStrip("corrupt", scene=_FakeScene(kind="smooth"),
                           props={self.store.PROP_KEY: [1.0]})
        curve = self.store.STORE.get_or_create(strip)
        self.assertEqual(curve.types, ["smooth"])
        self.assertEqual(curve.points, [(0.0, 1.0), (120.0, 1.0)])

    # ---------- 不介入既有数据 ----------

    def test_legacy_strip_without_types_still_reads_linear(self):
        """缺类型载荷 → 全线性，**不因默认=平滑而变形状**（默认是诞生值不是兜底值）。"""
        strip = _FakeStrip("legacy", scene=_FakeScene(kind="smooth"),
                           props={self.store.PROP_KEY: [0.0, 1.0, 40.0, 1.5, 120.0, 1.0]})
        curve = self.store.STORE.get(strip)
        self.assertEqual(curve.types, ["linear", "linear"])
        self.assertEqual(curve.points, [(0.0, 1.0), (40.0, 1.5), (120.0, 1.0)])

    def test_changing_the_default_does_not_touch_an_existing_curve(self):
        """改下拉**不回溯**：已落盘的曲线段类型一个不动（含缓存重读之后）。"""
        scene = _FakeScene(kind="smooth")
        strip = _FakeStrip("frozen", scene=scene)
        curve = self.store.STORE.get_or_create(strip)
        curve.move_point(1, rel_frame=120.0, speed=2.0)
        self.store.STORE.commit(strip, curve)
        self.assertEqual([int(v) for v in strip[self.store.TYPES_KEY]], [1])

        scene.seqvel.default_curve_type = "linear"
        self.assertEqual(self.store.STORE.get(strip).types, ["smooth"],
                         "缓存里的曲线不该被改默认这件事碰到")
        self.store.STORE.invalidate()
        self.assertEqual(self.store.STORE.get(strip).types, ["smooth"],
                         "从属性重读也不该回溯")
        self.assertEqual([int(v) for v in strip[self.store.TYPES_KEY]], [1])

    def test_insert_point_still_inherits_while_default_is_smooth(self):
        """拆段继承不受默认影响（默认只在 `Curve.default` 那一处生效）。"""
        strip = _FakeStrip("split", scene=_FakeScene(kind="smooth"),
                           props={self.store.PROP_KEY: [0.0, 1.0, 120.0, 2.0],
                                  self.store.TYPES_KEY: [0]})
        curve = self.store.STORE.get(strip)
        self.assertEqual(curve.types, ["linear"])
        curve.insert_point(60.0)
        self.assertEqual(curve.types, ["linear", "linear"],
                         "插点继承原段类型——哪怕场景默认是平滑")


if __name__ == "__main__":
    unittest.main()
