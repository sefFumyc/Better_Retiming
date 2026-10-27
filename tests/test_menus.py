# -*- coding: utf-8 -*-
"""右键菜单 draw 与静态层双语（ADR-0009 修订 R1）的回归。

**为什么单独一个文件**：v1.5.1 之前没有任何测试真正**画过**菜单——段菜单
``draw`` 里的 ``NameError``（``TYPE_LABELS`` 未导入）让"右键线段弹不出菜单"
在真机上存活了一个版本。这里用桩 layout 把两个菜单真画一遍，断言菜单项的
idname / 文案 / 打勾图标；再钉住"注册期固定的用户可见字段必须常驻英中双语"
（R1）与"运行层调用点不许硬编码中文"两条纪律。

不依赖 GPU / 真实窗口：菜单 ``draw`` 只碰 ``self.layout``。
"""

import types
import unittest

from seqvel.curve import CURVE_TYPES, FAST, LINEAR, SLOW, SMOOTH

from support import HAVE_BPY, FakeView2D, make_editor_context, make_strip, reset_scene

if HAVE_BPY:
    import bpy

    from seqvel import carrier, geometry, hover, interaction, i18n, status, store, ui


def setUpModule():
    # 断言英文运行层文案（ADR-0009：出厂 en，中文经 i18n 提供）
    if HAVE_BPY:
        i18n.set_language_for_test("en")


def tearDownModule():
    if HAVE_BPY:
        i18n.set_language_for_test(None)


if HAVE_BPY:

    class _OpItem(object):
        """layout.operator() 的替身返回值：接住 text/icon/自定义属性。"""

        def __init__(self, idname, text, icon):
            self.idname = idname
            self.text = text
            self.icon = icon

    class _MenuLayout(object):
        """菜单 draw 用的桩 layout：只记 operator 项。"""

        def __init__(self):
            self.items = []

        def operator(self, idname, text="", icon="NONE"):
            item = _OpItem(idname, text, icon)
            self.items.append(item)
            return item

        def label(self, text="", icon="NONE"):
            self.items.append(_OpItem("", text, icon))
            return self.items[-1]

    def _draw_menu(menu_cls, context=None):
        layout = _MenuLayout()
        fake_self = types.SimpleNamespace(layout=layout)
        menu_cls.draw(fake_self, context)
        return layout.items


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestSegmentMenuDraw(unittest.TestCase):
    def setUp(self):
        reset_scene()
        store.STORE.invalidate()
        status.clear_error()
        self.editor = bpy.context.scene.sequence_editor
        self.strip = make_strip(length=120)
        for strip in self.editor.strips:
            strip.select = False
        self.strip.select = True
        self.editor.active_strip = self.strip
        self.view2d = FakeView2D()
        self.context, _regions = make_editor_context(
            bpy.context.scene, self.view2d, active="WINDOW")

    def tearDown(self):
        status.clear_error()
        interaction._SEGMENT_TARGET = None

    def test_draw_lists_all_four_types_in_the_current_language(self):
        """段菜单必须真能画：四档全列出、文案走运行层、不抛（v1.5.0 NameError 回归）。"""
        items = _draw_menu(interaction.SEQVEL_MT_segment_menu, self.context)
        self.assertEqual(
            [item.idname for item in items],
            ["seqvel.set_segment_type"] * len(CURVE_TYPES),
            "四档都得是 set_segment_type 项")
        expected = [i18n.type_label(kind) for kind in CURVE_TYPES]
        self.assertEqual([item.text for item in items], expected)
        for item in items:
            self.assertEqual(item.icon, "NONE", "没打勾时谁都不许带勾图标")

    def test_draw_checks_the_current_segment_type(self):
        """右键选中的段是线性时，线性那一项要带勾（读不到目标就谁都不勾）。"""
        interaction._set_segment_target(self.strip, 0)
        items = _draw_menu(interaction.SEQVEL_MT_segment_menu, self.context)
        by_text = {item.text: item for item in items}
        self.assertEqual(by_text[i18n.type_label(LINEAR)].icon, "CHECKMARK")
        for kind in (SMOOTH, FAST, SLOW):
            self.assertEqual(by_text[i18n.type_label(kind)].icon, "NONE")

    def test_draw_carries_the_kind_payload_for_each_item(self):
        """每项必须把 kind 挂回操作符属性（菜单项凭它改类型）。"""
        items = _draw_menu(interaction.SEQVEL_MT_segment_menu, self.context)
        self.assertEqual(sorted(item.kind for item in items), sorted(CURVE_TYPES))


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestPointMenuDraw(unittest.TestCase):
    def test_draw_lists_delete_point_in_the_current_language(self):
        """点菜单第一版只有「删除点」，文案走运行层（v1.5.0 硬编码中文回归）。"""
        items = _draw_menu(interaction.SEQVEL_MT_point_menu)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].idname, "seqvel.delete_point")
        self.assertEqual(items[0].text, i18n.T("interaction.menu_delete_point"))


@unittest.skipUnless(HAVE_BPY, "需要 bpy（请用 blender --background --python 运行）")
class TestStaticLayerIsBilingual(unittest.TestCase):
    """ADR-0009 修订 R1：注册期固定的用户可见字段＝常驻英中双语。"""

    BILINGUAL_CLASSES = (
        # (模块, 类名, 是否检查 bl_description——Menu 类没有这个字段)
        ("interaction", "SEQVEL_OT_edit_speedline", True),
        ("interaction", "SEQVEL_OT_point_menu", True),
        ("interaction", "SEQVEL_MT_point_menu", False),
        ("interaction", "SEQVEL_MT_segment_menu", False),
        ("interaction", "SEQVEL_OT_set_segment_type", True),
        ("interaction", "SEQVEL_OT_delete_point", True),
        ("interaction", "SEQVEL_OT_delete_speedline", True),
        ("hover", "SEQVEL_OT_hover_cursor", True),
        ("ui", "SEQVEL_OT_toggle_raw_cache", True),
    )

    @staticmethod
    def _has_cjk(text):
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)

    @classmethod
    def _assert_bilingual(cls, testcase, label, text):
        testcase.assertTrue(text, "%s：文案不能为空" % label)
        testcase.assertTrue(
            any(ch.isascii() and ch.isalpha() for ch in text),
            "%s：缺英文半边（%r）" % (label, text))
        testcase.assertTrue(
            cls._has_cjk(text), "%s：缺中文半边（%r）" % (label, text))

    def test_operator_labels_and_descriptions_are_bilingual(self):
        modules = {"interaction": interaction, "hover": hover, "ui": ui}
        for module_name, class_name, has_desc in self.BILINGUAL_CLASSES:
            cls = getattr(modules[module_name], class_name)
            self._assert_bilingual(self, "%s.bl_label" % class_name, cls.bl_label)
            if has_desc:
                self._assert_bilingual(
                    self, "%s.bl_description" % class_name, cls.bl_description)

    def test_settings_property_names_and_descriptions_are_bilingual(self):
        for prop_id in ("hide_native_retiming", "show_hud", "speed_cap",
                        "default_curve_type"):
            prop = ui.SEQVEL_PG_settings.__annotations__[prop_id]
            keywords = prop.keywords
            self._assert_bilingual(
                self, "settings.%s.name" % prop_id, keywords["name"])
            self._assert_bilingual(
                self, "settings.%s.description" % prop_id, keywords["description"])
        language = ui.SEQVEL_AddonPreferences.__annotations__["language"]
        self._assert_bilingual(self, "language.name", language.keywords["name"])
        self._assert_bilingual(
            self, "language.description", language.keywords["description"])
