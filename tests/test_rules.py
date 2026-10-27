# -*- coding: utf-8 -*-
"""条带类型规则的单测（纯 Python）。"""

import unittest

from seqvel import i18n, rules


class _FakeStrip(object):
    """最小条带替身：rules 只看 type / select（身份用 id 比较）。"""

    def __init__(self, name, strip_type="IMAGE", select=False):
        self.name = name
        self.type = strip_type
        self.select = select


class TestVisibleStrips(unittest.TestCase):
    """可见 = 可抓（ADR-0005）：显示集合与命中集合共用这一个判定。"""

    def setUp(self):
        self.models = set()

        def has_model(strip):
            return strip.name in self.models

        self.has_model = has_model

    def _visible(self, strips, active=None):
        return rules.visible_strips(strips, active, self.has_model)

    def test_strip_with_curve_is_visible_even_unselected(self):
        """常开显示：建过速度曲线的条带，未选中也要画、要可抓。"""
        strip = _FakeStrip("with_curve")
        self.models.add("with_curve")
        self.assertEqual(self._visible([strip]), [strip])

    def test_strip_without_curve_needs_selection(self):
        """没建过线的条带：未选中不可见也不可抓（不存在隐形可抓的线）。"""
        plain = _FakeStrip("plain")
        self.assertEqual(self._visible([plain]), [])

    def test_selected_strip_without_curve_is_visible(self):
        """没建过线的条带选中后显示默认线，此时可抓（所见即可抓）。"""
        selected = _FakeStrip("selected", select=True)
        self.assertEqual(self._visible([selected]), [selected])

    def test_active_strip_without_curve_is_visible(self):
        """活动条带未选中（Blender 允许）也显示默认线。"""
        active = _FakeStrip("active")
        self.assertEqual(self._visible([active], active=active), [active])

    def test_non_source_strips_never_visible(self):
        """音频/效果/承载条带永远不可见不可抓。"""
        for strip_type in ("SOUND", "SPEED", "COLOR", "TRANSFORM"):
            strip = _FakeStrip("x", strip_type=strip_type, select=True)
            self.models.add("x")
            self.assertEqual(self._visible([strip]), [],
                             "%s 不该可见" % strip_type)

    def test_order_is_preserved(self):
        a = _FakeStrip("a")
        b = _FakeStrip("b", select=True)
        c = _FakeStrip("c")
        self.models.add("c")
        self.assertEqual(self._visible([a, b, c]), [b, c])

    def test_active_none_is_tolerated(self):
        strip = _FakeStrip("plain")
        self.assertEqual(self._visible([strip], active=None), [])


class TestCanEdit(unittest.TestCase):
    def setUp(self):
        # 文案断言钉住中文（ADR-0009：默认语言是 en，中文经 i18n 提供）
        i18n.set_language_for_test("zh")

    def tearDown(self):
        i18n.set_language_for_test(None)

    def test_source_strips_are_editable(self):
        for strip_type in rules.SOURCE_TYPES:
            allowed, message = rules.can_edit_type(strip_type)
            self.assertTrue(allowed, "%s 应该可编辑" % strip_type)
            self.assertEqual(message, "")

    def test_audio_strip_is_refused_with_reason(self):
        allowed, message = rules.can_edit_type("SOUND")
        self.assertFalse(allowed)
        self.assertIn("不影响音频", message)

    def test_effect_strips_are_refused(self):
        for strip_type in ("SPEED", "COLOR", "TRANSFORM", "MULTIPLY", "ADJUSTMENT"):
            allowed, message = rules.can_edit_type(strip_type)
            self.assertFalse(allowed)
            self.assertIn("源条带", message)

    def test_unknown_type_is_refused(self):
        allowed, message = rules.can_edit_type("SOMETHING_NEW")
        self.assertFalse(allowed)
        self.assertIn("不支持", message)
        allowed, _message = rules.can_edit_type(None)
        self.assertFalse(allowed)

    def test_audio_notes(self):
        self.assertIn("不影响音频", rules.audio_note_type("SOUND"))
        self.assertIn("音轨", rules.audio_note_type("MOVIE"))
        self.assertIsNone(rules.audio_note_type("IMAGE"))


class TestSamplingPolicy(unittest.TestCase):
    def test_scene_strips_render_subframes(self):
        # 场景条带把小数源位置交给 RE_RenderFrame 的 subframe → 真·无级变速、无代价
        self.assertEqual(rules.sampling_policy("SCENE"), rules.SAMPLING_SUBFRAME)
        self.assertIsNone(rules.sampling_note("SCENE"))

    def test_other_sources_round_to_nearest_frame(self):
        for strip_type in ("MOVIE", "IMAGE", "MOVIECLIP", "MASK", "META"):
            self.assertEqual(rules.sampling_policy(strip_type), rules.SAMPLING_SNAP)
            self.assertEqual(rules.sampling_note(strip_type), i18n.T("rules.note_snap"))

    def test_unknown_type_defaults_to_snap(self):
        self.assertEqual(rules.sampling_policy(None), rules.SAMPLING_SNAP)


class TestSubframeLimited(unittest.TestCase):
    """上游缺陷：Raw 缓存会把 scene 条带的子帧时间取整（见 docs/adr/0002）。"""

    def test_scene_with_raw_cache_is_limited(self):
        self.assertTrue(rules.subframe_limited(rules.SAMPLING_SUBFRAME, True))

    def test_scene_without_raw_cache_is_fine(self):
        self.assertFalse(rules.subframe_limited(rules.SAMPLING_SUBFRAME, False))

    def test_unknown_cache_state_is_not_reported_as_limited(self):
        # 读不到开关时不能凭空断定"受限"——面板会把异常原文单独写出来
        self.assertFalse(rules.subframe_limited(rules.SAMPLING_SUBFRAME, None))

    def test_snap_sampling_is_never_limited(self):
        # 本来就取整选帧的素材不吃子帧，缓存开与不开一个样
        self.assertFalse(rules.subframe_limited(rules.SAMPLING_SNAP, True))
        self.assertFalse(rules.subframe_limited(rules.SAMPLING_SNAP, False))

    def test_note_names_the_culprit(self):
        # 运行层文案（ADR-0009）：钉住中文版仍然点名"Raw 缓存"
        i18n.set_language_for_test("zh")
        try:
            note = i18n.T("rules.note_raw_cache")
        finally:
            i18n.set_language_for_test(None)
        self.assertIn("Raw", note)
        self.assertIn("缓存", note)


if __name__ == "__main__":
    unittest.main()
