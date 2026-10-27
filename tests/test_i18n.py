# -*- coding: utf-8 -*-
"""运行层文案表（ADR-0009）的单测（纯 Python，零 bpy）。

语义判据（grilling 定稿 + ADR-0009）：
- 语言模式出厂 **en**：读不到偏好（纯 Python / 未注册）一律回退 en；
- 每个键两种语言都必须有**非空**文案（新增文案漏配 = 当场红）；
- 中英文占位符一致：同一套 ``%`` 参数喂两种语言都不能崩（漏占位符 = 当场红）；
- 未知键原样返回（宁可漏译露 key，不许静默串文案）；
- **运行层调用点不许硬编码中文**（AST 静态扫描）——v1.5.0→v1.5.1 清理出的
  纪律：凡是 ``status.set_error`` / ``note_keymap`` / ``note_draw`` /
  ``self.report`` / 菜单 ``text=`` 里出现的中文，都绕过了语言切换。
"""

import ast
import os
import unittest

from seqvel import i18n

SEQVEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "seqvel")

#: 允许出现中文字符串字面量的模块（文案表本体；注释/文档串不在扫描范围）
_I18N_MODULE = "i18n.py"

#: 运行层出口：这些调用的**字面量**实参里不许出现中文
_RUNTIME_CALLS = {"set_error", "note_keymap", "note_draw", "report"}

#: 用户拍板"只留英文"的注册标签（侧栏标签 Velocity，v1.5.0 盘问定稿）
_ALLOWED_ENGLISH_LABELS = {"Velocity"}

_HAS_CJK = lambda text: any("\u4e00" <= ch <= "\u9fff" for ch in text)


class TestLanguageDefault(unittest.TestCase):
    def tearDown(self):
        i18n.set_language_for_test(None)

    def test_default_language_is_en(self):
        # 纯 Python 环境读不到插件偏好 → 回退出厂值 en
        self.assertEqual(i18n.current_language(), "en")

    def test_override_switches_language(self):
        i18n.set_language_for_test("zh")
        self.assertEqual(i18n.current_language(), "zh")

    def test_invalid_override_falls_back_to_en(self):
        i18n.set_language_for_test("fr")
        self.assertEqual(i18n.current_language(), "en")


class TestTableIntegrity(unittest.TestCase):
    """文案表完整性：两语言齐、占位符齐——漏配漏译当场红。"""

    def test_every_key_has_both_languages_nonempty(self):
        for key, (en, zh) in i18n._TEXT.items():
            self.assertTrue(en.strip(), "键 %s 缺英文文案" % key)
            self.assertTrue(zh.strip(), "键 %s 缺中文文案" % key)

    def test_format_placeholders_match_between_languages(self):
        import re
        pattern = re.compile(r"%(?:%|[-+ #0]*\d*(?:\.\d+)?[sdfeEgGxXosc])")
        for key, (en, zh) in i18n._TEXT.items():
            # %% 是转义不是占位符
            en_slots = [s for s in pattern.findall(en) if s != "%%"]
            zh_slots = [s for s in pattern.findall(zh) if s != "%%"]
            self.assertEqual(
                len(en_slots), len(zh_slots),
                "键 %s 的占位符数不一致：en=%r zh=%r" % (key, en_slots, zh_slots))
            for slot_en, slot_zh in zip(en_slots, zh_slots):
                self.assertEqual(
                    slot_en[-1], slot_zh[-1],
                    "键 %s 的占位符类型不一致：en=%r zh=%r" % (key, slot_en, slot_zh))

    def test_format_args_apply_in_both_languages(self):
        i18n.set_language_for_test("zh")
        try:
            self.assertEqual(i18n.T("regions.timeline", 800, 600),
                             "时间轴区域 800×600")
        finally:
            i18n.set_language_for_test(None)
        self.assertEqual(i18n.T("regions.timeline", 800, 600),
                         "Timeline region 800x600")

    def test_unknown_key_returns_key_verbatim(self):
        self.assertEqual(i18n.T("no.such.key"), "no.such.key")

    def test_type_label_covers_all_four_kinds_and_falls_back(self):
        for kind in ("linear", "smooth", "fast", "slow"):
            label = i18n.type_label(kind)
            self.assertTrue(label)
            self.assertNotEqual(label, "curve.type_%s" % kind)
        self.assertEqual(i18n.type_label("weird"), "weird")


def _seqvel_sources():
    for name in sorted(os.listdir(SEQVEL_DIR)):
        if name.endswith(".py") and name != _I18N_MODULE:
            with open(os.path.join(SEQVEL_DIR, name), encoding="utf-8") as fh:
                yield name, fh.read()


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


class TestNoHardcodedChineseInRuntimeLayer(unittest.TestCase):
    """运行层出口不许硬编码中文（ADR-0009 纪律的静态回归）。

    中文只能住在文案表（i18n.py）里；业务模块里出现在
    ``status.set_error`` / ``note_keymap`` / ``note_draw`` / ``report``
    或菜单 ``text=`` 字面量里的中文都**绕过了语言切换**——v1.5.0 交付后
    用户抓到的「删除点」就是这类漏网。注释与文档串不在此列（不上屏）。
    """

    def test_runtime_call_literals_contain_no_chinese(self):
        violations = []
        for filename, source in _seqvel_sources():
            tree = ast.parse(source, filename=filename)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                watched = _call_name(node) in _RUNTIME_CALLS
                text_kw = any(kw.arg == "text" for kw in node.keywords)
                if not (watched or text_kw):
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Constant)
                            and isinstance(sub.value, str)
                            and _HAS_CJK(sub.value)):
                        violations.append("%s:%d %r"
                                          % (filename, node.lineno, sub.value))
        self.assertEqual(violations, [],
                         "运行层出口出现硬编码中文（应走 i18n.T）：%s"
                         % "; ".join(violations))

    def test_no_module_or_class_level_chinese_string_constants(self):
        """模块级/类级的裸中文常量赋值一律红。

        这是 2026-09-16 敏感性注入留下的真实盲区：`clear_error()` 里一句
        ``_INJECTION_SENTINEL = "注入测试中文"`` 不经过任何运行层出口调用，
        上一条用例看不见它——审查（code-review）抓到后补的这条封死该形态。
        """
        violations = []
        for filename, source in _seqvel_sources():
            tree = ast.parse(source, filename=filename)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) \
                    else [node.target]
                # bl_label/bl_description 的裸字符串由双语用例管辖（R1：须双语）。
                # 类体内 `bl_label = ...` 的目标是 ast.Name（类命名空间），
                # 不是 ast.Attribute——别只查后者
                def _label_name(t):
                    if isinstance(t, ast.Name):
                        return t.id
                    if isinstance(t, ast.Attribute):
                        return t.attr
                    return None

                if any(_label_name(t) in ("bl_label", "bl_description")
                       for t in targets):
                    continue
                value = node.value
                if not (isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and _HAS_CJK(value.value)):
                    continue
                line = getattr(node, "lineno", 0)
                violations.append("%s:%d %r" % (filename, line, value.value))
        self.assertEqual(violations, [],
                         "模块/类级中文常量应住在文案表 i18n.py：%s"
                         % "; ".join(violations))

    def test_registered_labels_are_bilingual(self):
        """注册期固定的用户可见字段（bl_label/bl_description）＝常驻英中双语。

        例外：侧栏标签 Velocity（用户拍板只留英文）。
        """
        violations = []

        def check(filename, label, value):
            if label in ("bl_category",) or value in _ALLOWED_ENGLISH_LABELS:
                return
            has_ascii = any(ch.isascii() and ch.isalpha() for ch in value)
            if not (has_ascii and _HAS_CJK(value)):
                violations.append("%s %s=%r（缺 %s 半边）"
                                  % (filename, label, value,
                                     "中文" if has_ascii else "英文"))

        for filename, source in _seqvel_sources():
            tree = ast.parse(source, filename=filename)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    for stmt in node.body:
                        if not isinstance(stmt, ast.Assign):
                            continue
                        for target in stmt.targets:
                            if isinstance(target, ast.Attribute) and \
                                    target.attr in ("bl_label", "bl_description") \
                                    and isinstance(stmt.value, ast.Constant) \
                                    and isinstance(stmt.value.value, str):
                                check(filename, target.attr, stmt.value.value)
        self.assertEqual(violations, [],
                         "注册期字段必须常驻英中双语：%s" % "; ".join(violations))
