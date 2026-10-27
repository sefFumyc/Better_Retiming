# -*- coding: utf-8 -*-
"""接缝 2：Speed Control 落地映射的 headless 单测。

关键帧读回走 5.x 分层 action（``action.layers[*].strips[*].channelbags[*].fcurves``），
因此"写入的帧位置与数值 = 模型采样"可以被自动断言（调研文档里"读回不便"的
结论已被实测证伪）。
"""

import types
import unittest

from seqvel.curve import SMOOTH, Curve, to_abs_frame

from support import (HAVE_BPY, make_scene_strip, make_sound_strip, make_strip,
                     reset_scene)

# carrier 依赖 bpy：只在有 bpy 时导入，纯 Python 下整体 skip 而不是 import 失败。
if HAVE_BPY:
    from seqvel import VERSION_TAG, carrier, i18n


def setUpModule():
    # 本文件大量断言中文文案（ADR-0009：运行层默认 en，中文经 i18n 提供）
    if HAVE_BPY:
        i18n.set_language_for_test("zh")


def tearDownModule():
    if HAVE_BPY:
        i18n.set_language_for_test(None)


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestCarrier(unittest.TestCase):
    def setUp(self):
        import bpy

        from seqvel import store

        reset_scene()
        store.STORE.invalidate()
        self.bpy = bpy
        self.store = store
        self.strip = make_strip(length=120, name="target")
        self.curve = Curve([(0.0, 1.0), (40.0, 1.5), (120.0, 0.5)], 120.0)

    # ---------------------------------------------------------------- 创建
    def test_ensure_creates_bound_multiply_carrier(self):
        created = carrier.ensure_carrier(self.strip)
        self.assertIsNotNone(created)
        self.assertEqual(created.type, "SPEED")
        self.assertEqual(created.input_1, self.strip)
        self.assertEqual(created.speed_control, "MULTIPLY")
        self.assertFalse(created.use_frame_interpolate, "插件恒定关闭混合帧")
        self.assertTrue(created.name.startswith(carrier.CARRIER_PREFIX))

    def test_ensure_forces_interpolation_off_even_if_strip_had_it_on(self):
        # 旧 .blend 里可能留着开着混合帧的承载条带：每次落地都要把它写回来
        created = carrier.ensure_carrier(self.strip)
        created.use_frame_interpolate = True
        carrier.ensure_carrier(self.strip)
        self.assertFalse(created.use_frame_interpolate)

    def test_ensure_is_idempotent(self):
        first = carrier.ensure_carrier(self.strip)
        second = carrier.ensure_carrier(self.strip)
        self.assertEqual(first, second)
        speeds = [s for s in self.bpy.context.scene.sequence_editor.strips if s.type == "SPEED"]
        self.assertEqual(len(speeds), 1)

    def test_dedupe_is_by_input_binding_not_name(self):
        created = carrier.ensure_carrier(self.strip)
        created.name = "renamed_by_user"
        found = carrier.carrier_of(self.strip)
        self.assertIsNotNone(found)
        self.assertEqual(found, created)
        self.assertEqual(carrier.ensure_carrier(self.strip), created)

    def test_align_sets_channel_start_end(self):
        created = carrier.ensure_carrier(self.strip)
        self.strip.channel = 3
        self.strip.right_handle = self.strip.left_handle + 60
        carrier.align_carrier(self.strip, created)
        self.assertEqual(created.channel, 4)
        self.assertEqual(created.left_handle, self.strip.left_handle)
        self.assertEqual(created.right_handle, self.strip.right_handle)

    # ---------------------------------------------------------------- 写入
    def test_written_keys_match_model_sampling(self):
        diag = carrier.write_curve(self.strip, self.curve)
        self.assertEqual(diag["errors"], [])
        self.assertEqual(diag["written"], 3)
        keys = carrier.read_keys(carrier.carrier_of(self.strip))
        self.assertIsNotNone(keys, "应能读回关键帧")
        base = self.strip.left_handle
        expected = [(to_abs_frame(base, rel), speed) for rel, speed in self.curve.points]
        self.assertEqual([(int(frame), round(value, 6)) for frame, value, _i in keys], expected)

    def test_written_keys_are_linear(self):
        carrier.write_curve(self.strip, self.curve)
        keys = carrier.read_keys(carrier.carrier_of(self.strip))
        self.assertTrue(keys)
        self.assertTrue(all(interp == "LINEAR" for _f, _v, interp in keys))

    def test_eased_segment_writes_per_frame_keys(self):
        """缓动段必须逐帧落关键帧：只写端点的话 Blender 侧会把它拉回直线。"""
        eased = Curve([(0.0, 1.0), (4.0, 3.0)], 4.0, types=[SMOOTH])
        diag = carrier.write_curve(self.strip, eased)
        self.assertEqual(diag["errors"], [])
        keys = carrier.read_keys(carrier.carrier_of(self.strip))
        base = self.strip.left_handle
        expected = [to_abs_frame(base, rel) for rel in eased.keyframe_frames()]
        self.assertEqual([int(frame) for frame, _v, _i in keys], expected)
        mapped = {int(frame): round(value, 6) for frame, value, _i in keys}
        for rel in eased.keyframe_frames():
            self.assertAlmostEqual(mapped[to_abs_frame(base, rel)],
                                   round(eased.line_speed_at(rel), 6))
        self.assertIn(to_abs_frame(base, 2.0), mapped)  # 形状中间的帧确实写进去了

    def test_constant_speed_eased_curve_writes_two_keys(self):
        """恒速的缓动段只写两端点（ADR-0007 决定 6）：平坦的平滑段是常态。

        `written == keys_expected` 这条一致性由 carrier 自己断言（不一致会写进
        `diag["errors"]`），这里验的是**条数本身**降到 2——面板那行「关键帧 n 个」
        才不会因为"新曲线默认平滑"而在屏幕上写出 120 个全 1.0x 的帧。
        """
        flat = Curve([(0.0, 1.0), (120.0, 1.0)], 120.0, types=[SMOOTH])
        diag = carrier.write_curve(self.strip, flat)
        self.assertEqual(diag["errors"], [])
        self.assertEqual(diag["keys_expected"], 2)
        self.assertEqual(diag["written"], 2)
        keys = carrier.read_keys(carrier.carrier_of(self.strip))
        self.assertEqual([int(frame) for frame, _v, _i in keys],
                         [to_abs_frame(self.strip.left_handle, 0.0),
                          to_abs_frame(self.strip.left_handle, 120.0)])

    def test_write_clears_stale_keys(self):
        carrier.write_curve(self.strip, self.curve)
        shorter = Curve([(0.0, 1.2), (120.0, 1.2)], 120.0)
        diag = carrier.write_curve(self.strip, shorter)
        self.assertEqual(diag["cleared"], 3)
        keys = carrier.read_keys(carrier.carrier_of(self.strip))
        self.assertEqual(len(keys), 2)

    def test_write_reuses_same_carrier(self):
        first = carrier.carrier_of(self.strip)
        self.assertIsNone(first)
        carrier.write_curve(self.strip, self.curve)
        created = carrier.carrier_of(self.strip)
        carrier.write_curve(self.strip, self.curve)
        self.assertEqual(created, carrier.carrier_of(self.strip))
        speeds = [s for s in self.bpy.context.scene.sequence_editor.strips if s.type == "SPEED"]
        self.assertEqual(len(speeds), 1)

    def test_write_keeps_carrier_aligned_after_strip_resize(self):
        carrier.write_curve(self.strip, self.curve)
        self.strip.right_handle = self.strip.left_handle + 60
        carrier.write_curve(self.strip, self.curve)
        created = carrier.carrier_of(self.strip)
        self.assertEqual(created.left_handle, self.strip.left_handle)
        self.assertEqual(created.right_handle, self.strip.right_handle)

    def test_write_follows_strip_position(self):
        carrier.write_curve(self.strip, self.curve)
        moved = self.strip.left_handle + 25
        try:
            self.strip.left_handle = moved
        except Exception:
            self.skipTest("该版本 left_handle 只读")
        if self.strip.left_handle != moved:
            self.skipTest("该版本无法直接移动条带")
        carrier.write_curve(self.strip, self.curve)
        created = carrier.carrier_of(self.strip)
        self.assertEqual(created.left_handle, self.strip.left_handle)
        keys = carrier.read_keys(created)
        self.assertEqual(int(keys[0][0]), int(self.strip.left_handle))

    def test_diag_reports_progress(self):
        diag = carrier.write_curve(self.strip, self.curve)
        self.assertEqual(diag["points"], 3)
        self.assertTrue(diag["created"])
        self.assertTrue(diag["aligned"])
        self.assertFalse(diag["adjusted"], "新建时已经对齐，无需调整")
        self.assertTrue(diag["linear"])
        self.assertFalse(diag["retiming_reset"])
        self.assertEqual(diag["carrier"], carrier.CARRIER_PREFIX + self.strip.name)

    def test_carrier_follows_target_resize_automatically(self):
        """效果条带自身会跟随输入条带的时间范围（Blender 原生行为）。"""
        carrier.write_curve(self.strip, self.curve)
        created = carrier.carrier_of(self.strip)
        self.strip.right_handle = self.strip.left_handle + 60
        self.assertEqual(created.right_handle, self.strip.right_handle)

    def test_diag_reports_adjustment_when_carrier_drifts(self):
        carrier.write_curve(self.strip, self.curve)
        created = carrier.carrier_of(self.strip)
        created.channel = self.strip.channel + 3  # 人为弄偏
        diag = carrier.write_curve(self.strip, self.curve)
        self.assertTrue(diag["adjusted"])
        self.assertTrue(diag["aligned"])
        self.assertFalse(diag["created"], "第二次写入应复用承载条带")
        self.assertEqual(created.channel, self.strip.channel + 1)

    def test_write_to_missing_strip_is_reported_not_raised(self):
        diag = carrier.write_curve(None, self.curve)
        self.assertTrue(diag["errors"])
        self.assertIsNone(diag["carrier"])

    # ---------------------------------------------------- 写入收尾重建映射表
    def test_write_rebuilds_speed_map_after_writing_keys(self):
        """全量写入收尾必须显式让 Blender 重算速度映射表（ADR-0004 修订 R2）。

        那张表是**惰性构建、建一次就不再更新**的（R1 已量清），而全量写入是
        "清空 key → 逐个插 key"——中途会被建一次，此后写 key 不再触发重建。真机
        实测：曲线数据**一个字不改**，只走一遍 ``write_curve``，条带 93 帧里
        **89 帧**与"重建过的"不同；收尾补一次显式重建即 **93/93** 复原
        （``.scratch/tail-bug/findings.md``）。用户报的两条症状（拖末点对延长线
        没影响、拉长后尾巴乱跳）都是这一条。

        **顺序也是契约**：表必须按**最终** fcurve 重算，所以这次重建要排在写完
        key **和**置好插值之后。只断言"调用过"拦不住有人把它往前挪——挪到
        ``insert_keys`` 之前会按半成品算；挪到 ``set_linear_interpolation`` 之前，
        表读到的还是**默认插值**的曲线（那一步才把点改成 LINEAR），算出来的速度
        就不是画出来的那条线了。
        """
        order = []
        real_insert = carrier.insert_keys
        real_linear = carrier.set_linear_interpolation
        real_invalidate = carrier.invalidate_speed_map

        def spy_insert(*args, **kwargs):
            order.append("keys")
            return real_insert(*args, **kwargs)

        def spy_linear(*args, **kwargs):
            order.append("linear")
            return real_linear(*args, **kwargs)

        def spy_invalidate(strip):
            order.append("invalidate:%s" % getattr(strip, "name", "?"))
            return real_invalidate(strip)

        carrier.insert_keys = spy_insert
        carrier.set_linear_interpolation = spy_linear
        carrier.invalidate_speed_map = spy_invalidate
        try:
            diag = carrier.write_curve(self.strip, self.curve)
        finally:
            carrier.insert_keys = real_insert
            carrier.set_linear_interpolation = real_linear
            carrier.invalidate_speed_map = real_invalidate

        self.assertEqual(order,
                         ["keys", "linear", "invalidate:%s" % self.strip.name],
                         "重建必须排在写完 key、置好插值之后，且作用于目标条带")
        self.assertTrue(diag["invalidated"])
        self.assertEqual(diag["errors"], [])

    def test_write_reports_invalidate_failure(self):
        """重建失败不许静默：原因原文要进 ``errors``，且走既有那条 i18n 通路。

        断言整条文案而不是 `in` 子串：子串匹配拦不住有人另起一个模板
        （ADR-0004 修订 R2 要求与 ``relay_span`` 同一条通路，不新造第二套）。
        """
        real = carrier.invalidate_speed_map
        carrier.invalidate_speed_map = lambda _strip: "RuntimeError: 表炸了"
        try:
            diag = carrier.write_curve(self.strip, self.curve)
        finally:
            carrier.invalidate_speed_map = real

        self.assertFalse(diag["invalidated"])
        self.assertEqual(len(diag["errors"]), 1, "只该有这一个失败，不多不少")
        self.assertEqual(diag["errors"][0],
                         i18n.T("carrier.invalidate_failed", "RuntimeError: 表炸了"))

    def test_landing_paths_share_the_diag_contract(self):
        """两条落地路径给面板**同一个形状**——缺字段那行就显示成 ``?``。

        键集从 ``carrier._new_diag`` **现取**，不手抄：手抄的那份清单漏过
        ``retiming_reset``，于是这条本该抓"形状不一致"的断言对丢字段视而不见。
        """
        written = carrier.write_curve(self.strip, self.curve)
        relayed = carrier.relay_span(self.strip, self.curve)
        self.assertIsNotNone(relayed, "承载条带在，轻量路径必须落地")
        shared = set(carrier._new_diag(None, created=False))
        self.assertEqual(set(written) & shared, shared, "全量路径缺字段")
        self.assertEqual(set(relayed) & shared, shared, "轻量路径缺字段")

    # ---------------------------------------------------------------- Retiming
    def test_retiming_reset_is_noop_without_native_data(self):
        self.assertEqual(len(self.strip.retiming_keys), 0)
        self.assertFalse(carrier.reset_retiming(self.strip))

    def test_retiming_reset_skips_strips_without_retiming_support(self):
        self.assertEqual(carrier.reset_retiming(None), False)

    # ---------------------------------------------------------------- 删除
    def test_delete_carriers_removes_strip_and_keys(self):
        carrier.write_curve(self.strip, self.curve)
        created = carrier.carrier_of(self.strip)
        self.assertEqual(carrier.delete_carriers(self.strip), 1)
        self.assertIsNone(carrier.carrier_of(self.strip))
        self.assertIsNone(carrier.speed_curve(created), "承载被删后不应残留 fcurve")
        remaining = [s for s in self.bpy.context.scene.sequence_editor.strips if s.type == "SPEED"]
        self.assertEqual(remaining, [])

    def test_delete_carriers_on_strip_without_carrier(self):
        self.assertEqual(carrier.delete_carriers(self.strip), 0)

    # ---------------------------------------------------------------- 其它
    def test_carrier_report(self):
        from seqvel import rules

        carrier.write_curve(self.strip, self.curve)
        report = carrier.carrier_report(self.strip)
        self.assertEqual(report["mode"], "MULTIPLY")
        self.assertFalse(report["interpolate"], "恒定关闭混合帧")
        self.assertEqual(report["keys"], 3)
        self.assertEqual(report["bound"], self.strip.name)
        # 夹具是图像条带 → 渲染前取整；场景条带才会走子帧渲染
        self.assertEqual(report["sampling"], rules.SAMPLING_SNAP)
        self.assertEqual(report["sampling_note"], i18n.T("rules.note_snap"))
        # 取整选帧的素材不吃子帧，Raw 缓存开不开一个样 → 不该弹"无级受限"
        self.assertFalse(report["subframe_governed"])
        self.assertFalse(report["subframe_limited"])
        self.assertIsNone(report["subframe_note"])
        self.assertIsNone(carrier.carrier_report(None))

    # ------------------------------------------------------------------
    # 上游缺陷：Raw 缓存把场景条带的子帧取整
    # ------------------------------------------------------------------
    def test_scene_subframe_is_limited_by_raw_cache(self):
        from seqvel import rules

        strip = make_scene_strip()
        carrier.write_curve(strip, Curve([(0.0, 0.5), (60.0, 0.5)], duration=60.0))
        editor = self.bpy.context.scene.sequence_editor

        carrier.set_cache_raw(editor, True)
        report = carrier.carrier_report(strip)
        self.assertEqual(report["sampling"], rules.SAMPLING_SUBFRAME)
        self.assertTrue(report["subframe_governed"], "场景素材才有子帧可被吞")
        self.assertTrue(report["subframe_limited"], "场景素材 + Raw 缓存开 = 源时间被取整")
        self.assertEqual(report["subframe_note"], i18n.T("rules.note_raw_cache"))
        self.assertTrue(report["cache_raw"])

        carrier.set_cache_raw(editor, False)
        report = carrier.carrier_report(strip)
        self.assertTrue(report["subframe_governed"])
        self.assertFalse(report["subframe_limited"], "关掉缓存即恢复无级")
        self.assertIsNone(report["subframe_note"])
        self.assertFalse(report["cache_raw"])

    def test_cache_raw_state_roundtrip(self):
        editor = self.bpy.context.scene.sequence_editor
        original, error = carrier.cache_raw_state(editor)
        self.assertIsNone(error)
        self.assertIsInstance(original, bool)

        self.assertEqual(carrier.set_cache_raw(editor, True), (True, None))
        self.assertEqual(carrier.cache_raw_state(editor), (True, None))
        self.assertEqual(carrier.set_cache_raw(editor, False), (True, None))
        self.assertEqual(carrier.cache_raw_state(editor), (False, None))

    def test_cache_raw_state_reports_failure_instead_of_guessing(self):
        class Broken(object):
            @property
            def use_cache_raw(self):
                raise RuntimeError("RNA 不可用")

        value, error = carrier.cache_raw_state(Broken())
        self.assertIsNone(value, "读不到就不许猜一个值出来")
        self.assertIn("RNA 不可用", error)

    def test_cache_raw_state_without_editor(self):
        value, error = carrier.cache_raw_state(None)
        self.assertIsNone(value)
        self.assertIn("序列编辑器", error)
        self.assertFalse(carrier.set_cache_raw(None, True)[0])

    def test_target_strip_prefers_editable_source_strip(self):
        context = self.bpy.context
        editor = context.scene.sequence_editor
        editor.active_strip = self.strip
        self.assertEqual(carrier.target_strip(context), self.strip)

    def test_target_strip_skips_effect_strip_for_selected_source(self):
        context = self.bpy.context
        editor = context.scene.sequence_editor
        effect = carrier.ensure_carrier(self.strip)
        editor.active_strip = effect
        self.strip.select = True
        effect.select = True
        self.assertEqual(carrier.target_strip(context), self.strip)

    def test_target_strip_none_without_sequencer(self):
        class FakeContext(object):
            scene = None

        self.assertIsNone(carrier.target_strip(FakeContext()))

    def test_target_diagnosis_without_scene(self):
        class FakeContext(object):
            scene = None

        lines = carrier.target_diagnosis(FakeContext())
        self.assertEqual(len(lines), 1)
        self.assertIn("场景", lines[0])

    def test_target_diagnosis_without_sequencer(self):
        class FakeContext(object):
            scene = types.SimpleNamespace(name="Empty", sequence_editor=None)

        lines = carrier.target_diagnosis(FakeContext())
        self.assertEqual(len(lines), 1)
        self.assertIn("序列编辑器", lines[0])

    def test_target_diagnosis_surfaces_a_broken_editor(self):
        """读取失败必须写成文字。

        静默吞掉异常会把"确实没选中"和"根本读不出来"混成同一种表现（都是
        「活动条带：（无）」），用户与开发者都无从下手。
        """

        class BrokenEditor(object):
            @property
            def active_strip(self):
                raise RuntimeError("active boom")

            @property
            def strips(self):
                raise RuntimeError("strips boom")

        context = types.SimpleNamespace(
            scene=types.SimpleNamespace(name="S", sequence_editor=BrokenEditor()))
        self.assertIsNone(carrier.target_strip(context))
        lines = carrier.target_diagnosis(context)
        self.assertTrue(any("strips boom" in line for line in lines),
                        "条带列表读取失败要出现在诊断里：%r" % (lines,))
        self.assertTrue(any("active boom" in line for line in lines),
                        "活动条带读取失败也要出现在诊断里：%r" % (lines,))

    def test_target_diagnosis_handles_an_empty_editor(self):
        editor = self.bpy.context.scene.sequence_editor
        for strip in list(editor.strips):
            editor.strips.remove(strip)
        lines = carrier.target_diagnosis(self.bpy.context)
        self.assertEqual(len(lines), 1)
        self.assertIn("还没有条带", lines[0])

    def test_target_diagnosis_counts_and_hint_when_nothing_selected(self):
        editor = self.bpy.context.scene.sequence_editor
        for strip in editor.strips:
            strip.select = False
        editor.active_strip = None
        lines = carrier.target_diagnosis(self.bpy.context)
        self.assertIn("条带 1 条", lines[0])
        self.assertIn("选中 0 条", lines[0])
        self.assertIn("活动条带：（无）", lines[0])
        self.assertTrue(any("点一下" in line for line in lines[1:]),
                        "没选中时应提示怎么选：%r" % (lines,))

    def test_target_diagnosis_counts_a_selected_strip_that_cannot_be_edited(self):
        sound = make_sound_strip()
        if sound is None:
            self.skipTest("本机无法创建音频条带")
        editor = self.bpy.context.scene.sequence_editor
        for strip in editor.strips:
            strip.select = False
        sound.select = True
        editor.active_strip = sound
        lines = carrier.target_diagnosis(self.bpy.context)
        self.assertTrue(any("选中 1 条" in line for line in lines),
                        "选中数必须是实测值，不能恒报 0：%r" % (lines,))
        self.assertTrue(any("不可编辑" in line for line in lines),
                        "选中的是不可编辑条带时要直说原因：%r" % (lines,))

    def test_no_target_message_carries_version_tag(self):
        """拒绝文案必须带版本号。

        覆盖安装插件后不重启 Blender，内存里跑的还是旧模块——症状就是"明明修
        了却依然报同样的错"。文案里带版本，才能一眼分辨跑的是哪一版。
        """
        editor = self.bpy.context.scene.sequence_editor
        for strip in editor.strips:
            strip.select = False
        editor.active_strip = None
        message = carrier.no_target_message(self.bpy.context)
        self.assertIn("选中", message)
        self.assertIn(VERSION_TAG, message,
                      "没有版本号就无法判断当前跑的是哪一版：%r" % (message,))

    def test_audio_strip_is_refused_and_explained(self):
        sound = make_sound_strip()
        if sound is None:
            self.skipTest("本机无法创建音频条带")
        allowed, message = carrier.can_edit(sound)
        self.assertFalse(allowed)
        self.assertIn("不影响音频", message)
        self.assertIn("不影响音频", carrier.audio_note(sound))

    def test_can_edit_reports_missing_selection(self):
        allowed, message = carrier.can_edit(None)
        self.assertFalse(allowed)
        self.assertIn("选中", message)

    def test_movie_strip_audio_hint(self):
        class FakeMovieStrip(object):
            type = "MOVIE"

        self.assertIn("音轨", carrier.audio_note(FakeMovieStrip()))

    def test_carrier_constants(self):
        from seqvel import rules

        self.assertEqual(carrier.SPEED_MODE, "MULTIPLY")
        # 帧精度策略是"永不混合"：混合帧只是拿两帧叠出一个重影，场景素材本就连续
        self.assertFalse(carrier.USE_FRAME_INTERPOLATE)
        self.assertEqual(rules.sampling_policy("SCENE"), rules.SAMPLING_SUBFRAME)
        self.assertEqual(rules.sampling_policy("MOVIE"), rules.SAMPLING_SNAP)
        self.assertIsNone(rules.sampling_note("SCENE"))
        self.assertEqual(carrier.KEY_INTERPOLATION, "LINEAR")


if __name__ == "__main__":
    unittest.main()
