# -*- coding: utf-8 -*-
"""侧边栏面板与插件设置（VSE > 侧边栏(N) > Velocity）。

面板是唯一的信息出口：活动条带、承载条带现状、警告、错误与调试信息都在
这里集中显示。v1.5.0 重排（grilling 定稿）：

1. Language（语言模式，存插件偏好，出厂 en——ADR-0009）；
2. **设置**（显示 / 新建曲线）——正常用户最先看到的就是它们；
3. 目标条带信息 + 删除按钮；
4. 条件警告（不能编辑 / 音频 / Raw 缓存 guard / 绑定不一致 / 错误）；
5. **调试区**（下沉到底）：编辑中、承载报告、上次写入、键位健康、版本。

删除（grilling 定稿）：操作教程 box（能用这插件的人本来就懂 Vegas）、
"选择快照"与"绘制回调次数"两行纯排障产物（轨迹文件足够）。

文案两层（ADR-0009，修订 R1）：运行层走 :mod:`i18n` 随语言切换；本文件的
``bl_label`` / ``bl_category`` / operator 名等**静态层常驻英中双语**
（侧栏标签 Velocity 按用户定名只留英文）。
"""

import bpy

from . import VERSION_TAG, carrier, curve, i18n, ram, regions, status
from .curve import CURVE_TYPES
from .store import STORE

_WRAP_WIDTH = 30

#: 显示范围上限的两档（identifier = 百分数；CONTEXT.md「显示范围上限」）。
SPEED_CAPS = (("300", 3.0), ("1000", 10.0))
_CAP_BY_ID = dict(SPEED_CAPS)


# --------------------------------------------------------------------------
# 动态枚举项（Blender 只允许枚举的名称/说明走运行时回调 → 真双语；
# 布尔 Property 的 name/description 是注册期固定的，这就是"布尔改枚举"的原因）
# --------------------------------------------------------------------------

def _hide_retiming_items(_self, _context):
    # v1.5.2：项级说明给空串——tooltip＝属性级双语说明独自承担；项级说明走
    # RNA 缓存、切语言不保证重算（ADR-0009 修订 R2）。
    # 注意：本版 Blender 的动态枚举回调**不收二元组**（文档与行为偏差，
    # 同"default 只能整数下标"），最少三元组，说明位给 "" 即可。
    return (("ON", i18n.T("ui.set_hide_retiming_on"), ""),
            ("OFF", i18n.T("ui.set_hide_retiming_off"), ""))


def _show_hud_items(_self, _context):
    return (("ON", i18n.T("ui.set_hud_on"), ""),
            ("OFF", i18n.T("ui.set_hud_off"), ""))


def _curve_type_items(_self, _context):
    return tuple((kind, i18n.type_label(kind), "") for kind in CURVE_TYPES)


def _speed_cap_items(_self, _context):
    return tuple((ident, "%s%%" % ident, "") for ident, _value in SPEED_CAPS)


class SEQVEL_PG_settings(bpy.types.PropertyGroup):
    """场景级设置。hide_native_retiming / show_hud 原是 BoolProperty，
    v1.5.0 改动态枚举（双语选项说明，ADR-0009）——旧 .blend 读回时这两项
    重置一次（grilling 已接受的迁移代价）。

    name/description 是注册期固定的**用户可见**字段，按 ADR-0009 修订 R1
    常驻英中双语。下拉项的**名称**走运行时回调（随语言实时切换）；但项级
    **说明**自 v1.5.2 起一律给空串——它走 RNA 缓存、切语言不保证重算，
    且非空时会与属性级说明拼接成"英中英/英中中"重复（ADR-0009 修订 R2），
    tooltip 一律由属性级双语说明独自承担。
    """

    hide_native_retiming: bpy.props.EnumProperty(
        name="Hide native retiming while editing 编辑时隐藏原生 Retiming 显示",
        description=("Collapse the native retiming keyframe display on strips "
                     "edited with speed lines (display only; native data is "
                     "handled by the reset-rebuild policy). "
                     "用速度线编辑过的条带收起原生 Retiming 关键帧显示（只影响"
                     "显示，不改原生数据；原生数据由「重置重建」策略处理）"),
        items=_hide_retiming_items,
        # 动态枚举的 default 只能是**下标**（items=回调时 Blender 不收标识串）
        default=0,  # ON
    )
    show_hud: bpy.props.EnumProperty(
        name="Show speed percentages 显示速度百分比",
        description=("Always show a percentage next to every speed point; "
                     "when off, only the points being dragged show it "
                     "temporarily. "
                     "始终在每个速度点旁显示速度百分比；不勾选时只有拖动中的"
                     "那些点临时显示"),
        items=_show_hud_items,
        default=1,  # OFF
    )
    speed_cap: bpy.props.EnumProperty(
        name="Display range cap 显示范围上限",
        description=("Full scale of the speed ruler: affects the ruler, where "
                     "1.0x sits and the drag clamp. Curve data stores absolute "
                     "speeds, so this can change live. "
                     "速度标尺的满量程：影响标尺、1.0x 基线位置与拖动钳制。"
                     "曲线数据存的是绝对速度值，因此可实时改"),
        items=_speed_cap_items,
        default=0,  # 300%
    )
    default_curve_type: bpy.props.EnumProperty(
        name="Default segment curve type 默认段曲线类型",
        description=("Shape new speed curves' segments take. Only applies at "
                     "creation: existing curves are untouched; split segments "
                     "inherit the original type, merged segments keep the left "
                     "type (ADR-0007). "
                     "新建速度曲线的段形状。只在新建那一刻生效：已有曲线不动；"
                     "拆段继承原类型、并段取左段类型（ADR-0007）"),
        items=_curve_type_items,
        default=1,  # SMOOTH
    )


class SEQVEL_AddonPreferences(bpy.types.AddonPreferences):
    """插件偏好：语言模式住这里——**用户属性不是工程属性**，跨 .blend 持久。"""

    bl_idname = "seqvel"
    language: bpy.props.EnumProperty(
        name="Language 语言",
        description=("User interface language for the runtime layer (panel "
                     "text, menus, reports). Registration-time text (labels, "
                     "tooltips of operators and settings) is permanently "
                     "bilingual. "
                     "界面语言：只实时切换运行层文案（面板、右键菜单、报告）；"
                     "注册期固定文案（标签、操作符与设置的悬停说明）常驻英中双语"),
        items=(("en", "English", ""),
               ("zh", "中文", "")),
        default="en",
    )


def settings_of(scene):
    return getattr(scene, "seqvel", None)


def cap_of_settings(settings):
    """设置 → 显示范围上限（浮点）；设置缺失/标识非法回退出厂 3.0。"""
    ident = getattr(settings, "speed_cap", "300") if settings is not None else "300"
    return _CAP_BY_ID.get(str(ident), curve.SPEED_MAX)


def scene_cap(scene):
    """显示场景 → 显示范围上限。"""
    return cap_of_settings(settings_of(scene))


def sync_active_cap_scene(scene):
    """把显示场景的上限同步进纯模型层（绘制/命中/拖动的共同标尺）。"""
    curve.set_active_cap(scene_cap(scene))


def sync_active_cap(context):
    """按当前上下文的显示场景同步上限（interaction/overlay 入口用）。"""
    sync_active_cap_scene(regions.displayed_scene(context))


def hide_retiming(settings):
    """设置 → 布尔（设置缺失按"隐藏"处理，与旧默认一致）。"""
    return getattr(settings, "hide_native_retiming", "ON") == "ON"


def apply_retiming_display(strip):
    """按设置收起 / 放出原生 Retiming 显示（只动显示，不动数据）。"""
    if strip is None:
        return
    settings = settings_of(getattr(strip, "id_data", None))
    hide = True if settings is None else hide_retiming(settings)
    try:
        strip.show_retiming_keys = not hide
    except Exception:
        pass


def restore_retiming_display(strip):
    """删除速度线时把原生显示放回来。"""
    if strip is None:
        return
    try:
        strip.show_retiming_keys = True
    except Exception:
        pass


def _wrapped(layout, text, width=_WRAP_WIDTH):
    """长文案按字数折行（Blender 的 label 不自动换行）。"""
    body = str(text or "")
    while body:
        layout.label(text=body[:width])
        body = body[width:]


def _tag_redraw(context):
    """操作符改了会被面板读到的状态后，让面板立刻重画。"""
    area = getattr(context, "area", None)
    if area is not None:
        try:
            area.tag_redraw()
        except Exception:
            pass


class SEQVEL_OT_toggle_raw_cache(bpy.types.Operator):
    """切换本场景的 Raw 缓存（上游缺陷的补救入口）。

    刻意**不做成落地时自动关**：缓存是场景级设置，关掉会连带影响同场景里与
    速度线无关条带的性能。所以改成用户点一下才生效、可再点恢复——插件的立场
    是"不越权改用户的项目设置，但要把问题和出口都摆到面板上"。
    """

    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_idname = "seqvel.toggle_raw_cache"
    bl_label = "Toggle this scene's Raw cache 切换本场景 Raw 缓存"
    bl_description = ("Turn off this scene's Raw cache so scene strips render "
                      "subframes (stepless speed). The cache is a scene-level "
                      "setting: disabling costs a bit of memory and CPU. "
                      "关闭本场景序列编辑器的 Raw 缓存，恢复场景条带的子帧渲染"
                      "（无级变速）。缓存是场景级设置：关闭会略微增加内存与 CPU 开销")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = regions.displayed_scene(context)  # 与面板目标同一场景
        editor = getattr(scene, "sequence_editor", None)
        current, error = carrier.cache_raw_state(editor)
        if current is None:
            self.report({"ERROR"}, i18n.T("ui.raw_read_failed", error, VERSION_TAG))
            return {"CANCELLED"}
        ok, error = carrier.set_cache_raw(editor, not current)
        if not ok:
            self.report({"ERROR"}, i18n.T("ui.raw_write_failed", error, VERSION_TAG))
            return {"CANCELLED"}
        self.report({"INFO"}, i18n.T("ui.raw_toggled",
                                     i18n.T("ui.raw_now_off") if current else i18n.T("ui.raw_now_on"),
                                     i18n.T("ui.raw_restored") if current else i18n.T("ui.raw_eaten"),
                                     VERSION_TAG))
        _tag_redraw(context)
        return {"FINISHED"}


def _draw_subframe_guard(layout, report):
    """场景素材专用：子帧渲染会不会被本场景的 Raw 缓存吃掉（上游缺陷）。

    单独一段 + 按钮：这是**唯一**一处插件自身修不好的问题（缓存是场景级设置，
    不能自动替用户改），所以既要讲清楚、也要给出动作。按钮常驻——关掉之后
    还要能再点回来。

    不变式：``report`` 的缓存开关读的是承载条带所在场景（``editor_of(strip)``），
    而 ``strip`` 恒来自 ``carrier.target_strip(context)``、即面板正编辑的那个
    ``context.scene`` —— 所以下面按钮操作 ``context.scene.sequence_editor``
    与提示说的是同一个场景，不存在"显示 A 场景、按钮改 B 场景"的错位。
    """
    if not report.get("subframe_governed"):
        return
    box = layout.box()
    if report.get("cache_error"):
        box.label(text=i18n.T("ui.raw_cache_unreadable"), icon="ERROR")
        _wrapped(box, report["cache_error"])
        return
    raw = report.get("cache_raw")
    box.label(text=i18n.T("ui.raw_cache_state",
                          i18n.T("ui.raw_on") if raw else i18n.T("ui.raw_off")))
    if report.get("subframe_limited"):
        box.label(text=i18n.T("ui.subframe_limited"), icon="ERROR")
        _wrapped(box, report["subframe_note"])
    else:
        box.label(text=i18n.T("ui.subframe_ok"), icon="CHECKMARK")
    # 显式钉死调用方式：这段代码不依赖面板别处怎么改 operator_context
    #（本操作符没有 invoke，两条路径都落到 execute，行为一致）
    box.operator_context = "EXEC_DEFAULT"
    box.operator("seqvel.toggle_raw_cache",
                 text=i18n.T("ui.raw_disable") if raw else i18n.T("ui.raw_enable"))


def _draw_memory_preview(layout, shown_scene):
    """内存预览分区（ADR-0011）：范围、这次要花多少内存、现在手上有什么。

    这里就是全部出口：构建进度、构建到哪一帧、失效原因都在这一格子里。
    Vegas 的老习惯是"有没有 RAM 预览一眼可见"——本分区的尺寸是有意为之，
    别为了给别的分区腾地方把它压成一行。
    """
    state = ram.STATE
    start, end, source = ram.resolve_range(shown_scene)
    box = layout.box()
    box.label(text=i18n.T("ui.ram_section"))
    if shown_scene is None or getattr(shown_scene, "sequence_editor",
                                      None) is None:
        box.label(text=i18n.T("ui.ram_no_cache"))
        return
    if start is None:
        box.label(text=i18n.T("ui.ram_no_range"), icon="INFO")
        return
    source_key = ("ui.ram_source_preview" if source == ram.SOURCE_PREVIEW
                  else "ui.ram_source_scene")
    box.label(text=i18n.T("ui.ram_range", start, end, i18n.T(source_key)))
    if state["estimate_mb"] and state["limit_mb"]:
        box.label(text=i18n.T("ui.ram_estimate",
                              int(state["estimate_mb"]), state["limit_mb"]))
    elif state["limit_mb"]:
        box.label(text=i18n.T("ui.ram_idle"))
    if state["building"]:
        pct = int(round(ram.progress(start, end, state["frame"]) * 100))
        box.label(text=i18n.T("ui.ram_building", pct, state["frame"]),
                  icon="PLAY")
        return
    outcome = state["outcome"]
    watermark = state["watermark"]
    if state["built"]:
        if outcome == ram.OUTCOME_FULL:
            box.label(text=i18n.T("ui.ram_full", watermark), icon="ERROR")
        elif outcome == ram.OUTCOME_STOPPED:
            box.label(text=i18n.T("ui.ram_stopped", watermark), icon="CHECKMARK")
        else:
            box.label(text=i18n.T("ui.ram_built", watermark,
                                  int(state["cache_mb"])), icon="CHECKMARK")
        return
    if outcome == ram.OUTCOME_INVALIDATED:
        box.label(text=i18n.T("ui.ram_invalidated"), icon="INFO")
        return
    if outcome == ram.OUTCOME_ABORTED:
        box.label(text=i18n.T("ui.ram_aborted"), icon="ERROR")
        return
    box.label(text=i18n.T("ui.ram_idle"), icon="INFO")


def rules_sampling_label(report):
    """承载报告的取帧策略显示名（内部标识 → 运行层文案）。"""
    return i18n.T("rules.sampling_subframe"
                  if report.get("sampling") == "subframe"
                  else "rules.sampling_snap")


def segment_type_summary(curve_model):
    """段类型分布文案（面板常显；没有曲线时说明"还没有数据"）。

    面板是唯一信息出口：断了哪一段的形状改了、改成了什么，必须看得见，
    不允许"右键选了但看不出生效"。
    """
    if curve_model is None:
        return i18n.T("ui.no_data")
    counts = curve_model.type_counts()
    return " · ".join("%s %d" % (i18n.type_label(kind), counts.get(kind, 0))
                      for kind in CURVE_TYPES)


class SEQVEL_PT_speedline(bpy.types.Panel):
    # 侧栏标签 "Velocity"：用户拍板只留英文（R1 例外，见 _ALLOWED_ENGLISH_LABELS）
    bl_label = "Velocity"
    bl_idname = "SEQVEL_PT_speedline"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Velocity"

    def draw(self, context):
        layout = self.layout
        strip = carrier.target_strip(context)
        shown_scene = regions.displayed_scene(context)
        settings = settings_of(shown_scene)

        # ---- 1. 语言模式（存插件偏好，出厂 en；ADR-0009） ----------------
        prefs = i18n.preferences()
        if prefs is not None:
            layout.prop(prefs, "language", text=i18n.T("ui.language"))

        # ---- 2. 设置（上移；正常用户先看到设置） -------------------------
        if settings is not None:
            column = layout.column(heading=i18n.T("ui.settings_display"))
            column.prop(settings, "hide_native_retiming", expand=True)
            column.prop(settings, "show_hud", expand=True)
            column.prop(settings, "speed_cap",
                        text=i18n.T("ui.set_speed_cap"))
            # 建线策略与显示分开一列：它不是显示设置，而且只作用于"新曲线诞生"那一刻
            #（见 docs/adr/0007-default-segment-type-read-once.md）。
            birth = layout.column(heading=i18n.T("ui.settings_birth"))
            birth.prop(settings, "default_curve_type",
                       text=i18n.T("ui.set_default_type"))

        # ---- 3. 目标条带 + 删除 ------------------------------------------
        target_ok, target_reason = carrier.can_edit(strip)
        box = layout.box()
        if strip is not None:
            box.label(text=i18n.T("ui.target_strip", strip.name))
            box.label(text=i18n.T("ui.target_meta",
                                  strip.type, strip.channel,
                                  int(carrier.strip_duration(strip))))
            box.label(text=i18n.T("ui.segment_types",
                                  segment_type_summary(STORE.get(strip))))
            if STORE.get(strip) is None:
                # 只显示给"还没建线"的条带（grilling 定稿：懂 Vegas 的人
                # 不需要常驻教程，但第一次见到线的人至少要知道线能抓）。
                _wrapped(box, status.hint_idle())
        else:
            box.label(text=i18n.T("ui.target_none"))
            # 只说「（无）」帮不上忙：把编辑器里的真实状态列出来。
            for line in carrier.target_diagnosis(context):
                _wrapped(box, line, width=42)
        row = layout.row(align=True)
        # 直接操纵（ADR-0005）下没有「编辑」按钮：唯一入口是时间轴上直接
        # 按住速度线。面板只剩删除。
        row.operator("seqvel.delete_speedline",
                     text=i18n.T("ui.delete_button"))

        # ---- 内存预览（Shift+B；唯一入口也是键盘） -----------------------
        _draw_memory_preview(layout, shown_scene)

        # ---- 4. 条件警告（行动入口，不算调试） ---------------------------
        active_scene = getattr(context, "scene", None)
        if shown_scene is not None and active_scene is not None and shown_scene != active_scene:
            layout.label(text=i18n.T("ui.scene_mismatch",
                                     shown_scene.name, active_scene.name), icon="INFO")

        if strip is not None and not target_ok:
            # 选中的是不能编辑的条带（音频 / 效果条带如插件自己的 SPD_）：明说原因
            refusal = layout.box()
            refusal.label(text=i18n.T("ui.refuse_title"), icon="INFO")
            _wrapped(refusal, target_reason)
        else:
            note = carrier.audio_note(strip)
            if note:
                audio_box = layout.box()
                audio_box.label(text=i18n.T("ui.audio_note"), icon="SPEAKER")
                _wrapped(audio_box, note)

        report = carrier.carrier_report(strip) if strip is not None else None
        if report is not None:
            if report.get("bound") != strip.name:
                layout.label(text=i18n.T("ui.bound_mismatch"), icon="ERROR")
            _draw_subframe_guard(layout, report)

        if status.STATE["error"]:
            error_box = layout.box()
            error_box.label(text=i18n.T("ui.error_title"), icon="ERROR")
            _wrapped(error_box, status.STATE["error"])

        # ---- 5. 调试区（下沉到底） ---------------------------------------
        diag_zone = layout.box()
        if status.STATE["editing"]:
            editing = diag_zone.column(align=True)
            editing.label(text=i18n.T("ui.editing_now",
                                      status.STATE["editing"], status.mode_label()))
            # 自查这行：编辑中"怎么点都没反应"最常见的原因是坐标来源不对
            #（操作符拿到的 view2d 不是时间轴的）。把实际用的区域和最近一次
            # 命中的结果摆出来，出错时一眼看出卡在哪一层。
            editing.label(text=i18n.T("ui.hit_line",
                                      regions.region_label(context),
                                      status.STATE["last_hit"] or "—"))
        if report is not None:
            keys = (i18n.T("ui.keys_unreadable") if report["keys"] is None
                    else i18n.T("ui.keys_count", report["keys"]))
            diag_zone.label(text=i18n.T("ui.carrier_line1",
                                        report["name"], report["channel"],
                                        report["start"], report["end"], keys))
            diag_zone.label(text=i18n.T("ui.carrier_line2",
                                        report["mode"],
                                        rules_sampling_label(report)))
            if report.get("sampling_note"):
                _wrapped(diag_zone, report["sampling_note"])
        elif strip is not None:
            diag_zone.label(text=i18n.T("ui.carrier_pending"))

        diag = status.STATE["diag"]
        if diag:
            diag_zone.label(text=i18n.T("ui.diag_line",
                                        diag.get("points", "?"),
                                        diag.get("written", "?"),
                                        i18n.T("ui.yes") if diag.get("aligned") else i18n.T("ui.no"),
                                        i18n.T("ui.yes") if diag.get("retiming_reset") else i18n.T("ui.no")))

        # 覆盖安装后不重启 Blender，跑的还是内存里的旧模块——"装完仍报同样的错"
        # 时先看这行：不是当前版本号，就说明需要重启 Blender。
        diag_zone.label(text=i18n.T("ui.version_line", VERSION_TAG), icon="INFO")

        # 键位挂载健康位（v1.2.3）：3/3 = 三个入口都挂在 Sequencer 键位表上。
        # 这行不是 3/3，就说明直接操纵的入口残了——"按了没反应"先看这里。
        if status.STATE["keymap"]:
            diag_zone.label(text=i18n.T("ui.keymap_line", status.STATE["keymap"]),
                            icon="KEYINGSET")


_CLASSES = (SEQVEL_PG_settings, SEQVEL_AddonPreferences,
            SEQVEL_OT_toggle_raw_cache, SEQVEL_PT_speedline)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.seqvel = bpy.props.PointerProperty(type=SEQVEL_PG_settings)
    status.set_hint(status.hint_idle())


def unregister():
    if hasattr(bpy.types.Scene, "seqvel"):
        del bpy.types.Scene.seqvel
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
