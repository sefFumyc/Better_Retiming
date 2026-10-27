# -*- coding: utf-8 -*-
"""GUI 冒烟测试：在**真实窗口**里验证装配、区域定位与入口路径。

headless 测试跑不到的部分由这里覆盖：

- 键位挂载、真实 area/region 上下文下的目标识别、工具鼠标路径是否穿透；
- **区域定位**：真机实测（5.2.1）里 ``region.view2d`` 在所有区域类型上都不是
  None（HEADER / UI / TOOLS / CHANNELS / FOOTER / PREVIEW 全都有，连 1x1 的
  折叠区域也有），只是坐标系不是"帧 / 通道"。用错区域做命中检测，同一条线、
  同一个像素也会落空 —— 表现就是"进了编辑态却怎么点都没反应"。这里的检查用
  **真实的区域对象**验证：从侧栏 / 工具栏区域出发也必须定位到时间轴，
  并且侧栏入口进来后仍能拖得动线。

它不注入鼠标键盘、不抢焦点，跑完自动退出：

    "D:\\Tools\\blender-5.2.1-windows-x64\\blender.exe" --factory-startup \\
        --python tools/gui_smoke.py -- --report %TEMP%\\seqvel_gui_smoke.txt

Windows 上 GUI 进程的 stdout 不接控制台，所以结果写进报告文件；退出码 0/1 可用。
"""

import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bpy  # noqa: E402

DEFAULT_REPORT = os.path.join(tempfile.gettempdir(), "seqvel_gui_smoke.txt")

_REPORT = DEFAULT_REPORT
if "--report" in sys.argv:
    _REPORT = sys.argv[sys.argv.index("--report") + 1]

_FAILURES = []

#: 非时间轴、但 view2d 照样存在的区域类型（真机实测）
NON_TIMELINE_REGIONS = ("UI", "TOOLS", "CHANNELS", "HEADER", "PREVIEW", "FOOTER")


def say(line):
    print("[gui_smoke] %s" % line, flush=True)
    try:
        with open(_REPORT, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def check(label, ok, detail=""):
    line = ("PASS  " if ok else "FAIL  ") + label
    if detail and not ok:
        line += " | " + str(detail)
    if not ok:
        _FAILURES.append(label)
    say(line)
    return ok


class _LayoutRecorder(object):
    """UILayout 替身：只记录标签，用来断言面板文案。"""

    def __init__(self):
        self.labels = []
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
        return None

    def operator(self, idname, text=None):
        return None


class _MenuItem(object):
    """菜单项替身：接住 operator() 的 text/icon 与菜单项回挂的属性。"""

    def __init__(self, idname, text, icon):
        self.idname = idname
        self.text = text
        self.icon = icon


class _MenuLayout(object):
    """菜单 draw 用的 UILayout 替身：只记 operator 项（v1.5.1 菜单内容断言）。"""

    def __init__(self):
        self.items = []

    def operator(self, idname, text="", icon="NONE"):
        item = _MenuItem(idname, text, icon)
        self.items.append(item)
        return item

    def label(self, text="", icon="NONE"):
        self.items.append(_MenuItem("", text, icon))
        return self.items[-1]


class _Event(object):
    """事件替身。同时可带区域局部坐标与窗口绝对坐标（真机两者都有）。"""

    def __init__(self, kind, x=0.0, y=0.0, value="PRESS", window=None):
        self.type = kind
        self.value = value
        self.mouse_region_x = float(x)
        self.mouse_region_y = float(y)
        if window is not None:
            self.mouse_x = float(window[0])
            self.mouse_y = float(window[1])


class _OperatorStub(object):
    """bpy 操作符不能直接实例化：借用真实方法 + 把 report 收进列表。"""

    def __init__(self):
        self.reports = []
        self.strip = None
        self.curve = None

    def report(self, level, message):
        self.reports.append((set(level), message))


def _find_area(screen, kind):
    for area in screen.areas:
        if area.type == kind:
            return area
    return None


def _media_png():
    path = os.path.join(tempfile.gettempdir(), "seqvel_gui_smoke", "media.png")
    if not os.path.exists(path):
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        image = bpy.data.images.new("seqvel_gui_smoke_media", 32, 32)
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
    return path


def _quitting():
    try:
        bpy.app.timers.register(lambda: os._exit(1 if _FAILURES else 0),
                                first_interval=3.0)
        bpy.ops.wm.quit_blender()
    except Exception:
        os._exit(1 if _FAILURES else 0)
    return None


def run():
    try:
        import types

        import seqvel
        from seqvel import (carrier, geometry, i18n, interaction, ram, regions,
                            status, store, ui)

        operator = interaction.SEQVEL_OT_edit_speedline
        borrowed = ("invoke", "execute", "_start", "modal", "_modal", "cancel",
                    "_begin_drag", "_end_action", "_apply_drag",
                    "_release_without_drag", "_insert_point", "_commit",
                    "_finish", "_cleanup",
                    "_capture_selection", "_restore_selection",
                    "_retune", "_header_text", "_drag_indices",
                    "_view2d", "_base_frame",
                    "_channel", "_mouse")
        for name in borrowed:
            missing = not hasattr(operator, name)
            check("操作符方法可借用：%s" % name, not missing)
            if not missing:
                setattr(_OperatorStub, name, getattr(operator, name))
        # 替身必须覆盖操作符的**全部**方法：漏一个，新方法里的 AttributeError 会被
        # invoke 的容错吞成 PASS_THROUGH 假红（真机表现就是"按了没反应"）。
        # test_session 有同款防线，这里补上——它与那处是同一个坑的两端。
        real_methods = {name for name in vars(operator)
                        if not name.startswith("__") and callable(getattr(operator, name))}
        check("替身覆盖操作符的全部方法", not (real_methods - set(borrowed)),
              "漏了：%s" % sorted(real_methods - set(borrowed)))

        say("=== %s | blender %s | background=%s ==="
            % (seqvel.VERSION_TAG, bpy.app.version_string, bpy.app.background))
        check("在真实窗口里运行", not bpy.app.background)

        # 用户偏好里若已启用本插件，Blender 启动时就替我们注册过了 —— 此时再调
        # ``register()`` 会撞 `ValueError: already registered as a subclass`。
        # 那是**没加 --factory-startup** 的环境问题，不是插件的错（见文件头的跑法），
        # 所以先说人话，别让它伪装成一串注册失败的栈。
        if getattr(bpy.types, "SEQVEL_PG_settings", None) is not None:
            raise RuntimeError(
                "seqvel 已被用户偏好预先注册：本冒烟必须加 --factory-startup 再跑，"
                "否则 register() 必然撞 'already registered as a subclass'")

        seqvel.register()
        check("插件注册成功", getattr(bpy.types, "SEQVEL_PT_speedline", None) is not None)

        addons = bpy.context.window_manager.keyconfigs.addon
        keymap = addons.keymaps.get("Sequencer") if addons else None
        ids = [item.idname for item in (keymap.keymap_items if keymap else [])]
        ok_keymap = (keymap is not None
                     and keymap.space_type == "SEQUENCE_EDITOR"
                     and keymap.region_type == "WINDOW"
                     and ids.count("seqvel.edit_speedline") == 2  # 普通 + Ctrl 微调（R9）
                     and "seqvel.point_menu" in ids
                     and "seqvel.hover_cursor" in ids
                     and "seqvel.delete_point" not in ids)
        check("直接操纵键位挂在 Sequencer（SEQUENCE_EDITOR/WINDOW）",
              ok_keymap,
              "%s / %s / %d 项 %r" % (getattr(keymap, "space_type", None),
                                     getattr(keymap, "region_type", None),
                                     len(ids), ids))

        check("段菜单与「设类型」算子已注册",
              getattr(bpy.types, "SEQVEL_MT_segment_menu", None) is not None
              and hasattr(bpy.ops.seqvel, "set_segment_type")
              and "seqvel.set_segment_type" not in ids,
              "菜单=%s" % (getattr(bpy.types, "SEQVEL_MT_segment_menu", None),))

        # [ADR-0011] 内存预览：Shift+B。它必须只挂**编辑器**键位表——工具键位表
        # 先于编辑器表求值（R8），混进去就意味着时间线上每一次普通左键都要先
        # 过一遍构建算子。这里把"挂在哪、怎么按、有几条"全部钉住。
        check("内存预览算子已注册",
              getattr(bpy.types, "SEQVEL_OT_build_memory_preview", None)
              is not None
              and hasattr(bpy.ops.seqvel, "build_memory_preview"))
        build_items = [item for item in (keymap.keymap_items if keymap else [])
                       if item.idname == "seqvel.build_memory_preview"]
        # 注意：修饰键在 RNA 里是 **int**（1/0），不是 bool —— 断言必须取真假值，
        # 写 ``is True`` 会把一个正确的键位项判成错的。
        ok_build_key = (len(build_items) == 1
                        and build_items[0].type == "B"
                        and build_items[0].value == "PRESS"
                        and bool(build_items[0].shift)
                        and not bool(build_items[0].ctrl)
                        and not bool(build_items[0].alt))
        check("Shift+B 挂在编辑器键位表（且只有这一条）", ok_build_key,
              [(i.type, i.value, i.shift, i.ctrl, i.alt) for i in build_items])
        tool_ids = []
        for tool_map_name in seqvel.TOOL_KEYMAP_NAMES:
            tool_map = addons.keymaps.get(tool_map_name) if addons else None
            tool_ids += [item.idname
                         for item in (tool_map.keymap_items if tool_map else [])]
        check("构建算子不进工具键位表（每次点击都不该过它）",
              "seqvel.build_memory_preview" not in tool_ids, tool_ids)

        # v1.5.1：菜单必须**真能画**。v1.5.0 的段菜单 draw 里 NameError
        # （TYPE_LABELS 未导入）让"右键线段弹不出菜单"在真机存活了一个版本——
        # 只断言"菜单已注册/被调用"拦不住它，必须把菜单项真画出来。
        from seqvel import interaction as _interaction

        def _draw_menu(menu_cls):
            # draw 只读 self.layout；context 仅在"有右键目标"时才被碰到
            layout = _MenuLayout()
            menu_cls.draw(types.SimpleNamespace(layout=layout), None)
            return layout.items

        seg_items = _draw_menu(_interaction.SEQVEL_MT_segment_menu)
        seg_texts = sorted(item.text for item in seg_items)
        check("段菜单真画得出四档（en）",
              [item.idname for item in seg_items] == ["seqvel.set_segment_type"] * 4
              and seg_texts == ["Fast", "Linear", "Slow", "Smooth"],
              "items=%r" % ([(i.idname, i.text) for i in seg_items],))
        pt_items = _draw_menu(_interaction.SEQVEL_MT_point_menu)
        check("点菜单真画得出删除项（en）",
              len(pt_items) == 1
              and pt_items[0].idname == "seqvel.delete_point"
              and pt_items[0].text == "Delete Point",
              "items=%r" % ([(i.idname, i.text) for i in pt_items],))

        # 票 04：装饰从 def 挪到了注册期——真机上必须仍然是 persistent 的那一个，
        # 否则用户一打开 .blend，R8 的自愈就只剩"注册那一次"。
        handler = seqvel._reregister_keymap_on_load
        load_post = list(bpy.app.handlers.load_post)
        marked = hasattr(handler, "_bpy_persistent")
        check("「载入后自愈键位」挂在 load_post 且标了 persistent",
              handler in load_post and marked,
              "load_post 共 %d 项，命中=%s，persistent 标记=%s"
              % (len(load_post), handler in load_post, marked))

        screen = bpy.context.screen
        area = _find_area(screen, "SEQUENCE_EDITOR")
        if area is None:
            area = _find_area(screen, "VIEW_3D") or screen.areas[-1]
            area.type = "SEQUENCE_EDITOR"
            try:
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
            except Exception as exc:
                say("redraw_timer 不可用（不影响判定）：%s" % exc)

        # 侧栏（UI 区域）就是面板按钮 / 面板上 Alt+S 的 context.region —— 先把它
        # 展开，才能复核用户实际走的那条路（工厂布局下 UI 区域是 1x1）。展开会
        # 改变各区域尺寸，所以必须在取 region / by_type **之前**做。
        try:
            space = area.spaces.active
            if hasattr(space, "show_region_ui"):
                space.show_region_ui = True
        except Exception as exc:
            say("展开侧栏失败（面板按钮路径复核会失败）：%s" % exc)

        scene = bpy.context.scene
        if scene.sequence_editor is None:
            scene.sequence_editor_create()
        editor = scene.sequence_editor

        region = None
        for candidate in area.regions:
            if candidate.type == "WINDOW":
                region = candidate
                break
        check("拿到真实 area/region", region is not None,
              "%s / %s" % (getattr(area, "type", None), getattr(region, "type", None)))

        by_type = {}
        for candidate in area.regions:
            by_type.setdefault(candidate.type, candidate)
        say("区域：%s" % ", ".join(
            "%s(%dx%d@%d,%d)" % (name, r.width, r.height, r.x, r.y)
            for name, r in sorted(by_type.items())))

        strip = editor.strips.new_image(name="gui_smoke_clip", filepath=_media_png(),
                                        channel=1, frame_start=1)
        strip.right_handle = strip.left_handle + 100

        with bpy.context.temp_override(window=bpy.context.window, screen=screen,
                                       area=area, region=region):
            real = bpy.context
            context = real
            strip.select = True
            editor.active_strip = strip
            check("真实上下文里识别到选中条带",
                  carrier.target_strip(context) == strip)
            lines = carrier.target_diagnosis(context)
            check("诊断行报告真实选中数",
                  any(("选中 1 条" in line) or ("1 selected" in line)
                      for line in lines), lines)

            stub = _OperatorStub()
            result = stub.invoke(context, _Event("LEFTMOUSE", 99999.0, 99999.0))
            check("工具点击空白处穿透（不吃掉点击）",
                  result == {"PASS_THROUGH"} and not stub.reports, result)

            strip.select = False
            editor.active_strip = None
            message = carrier.no_target_message(context)
            check("拒绝文案带实时状态",
                  ("选中 0 条" in message) or ("0 selected" in message), message)
            check("拒绝文案带版本号", seqvel.VERSION_TAG in message, message)

            layout = _LayoutRecorder()
            ui.SEQVEL_PT_speedline.draw(types.SimpleNamespace(layout=layout), context)
            check("面板显示当前版本",
                  any(seqvel.VERSION_TAG in text for text in layout.labels),
                  [t for t in layout.labels if "插件" in t])
            check("面板说明为什么没有目标",
                  ("选中 0 条" in "".join(layout.labels))
                  or ("0 selected" in "".join(layout.labels)),
                  list(layout.labels))

            # ------------------------------------------------------------------
            # 内存预览面板分区（ADR-0011）：必须**真画得出**，且语言不跟着偏好走
            # （否则中文模式下这些断言会静默失效——逐字断言的前提是钉住语言）
            # ------------------------------------------------------------------
            ram.reset()
            _lang = i18n._LANG_OVERRIDE
            i18n._LANG_OVERRIDE = i18n.EN
            try:
                range_start, range_end, range_source = ram.resolve_range(
                    real.scene)
                want_section = i18n.T("ui.ram_section")
                want_range = i18n.T(
                    "ui.ram_range", range_start, range_end,
                    i18n.T("ui.ram_source_scene"
                           if range_source == ram.SOURCE_SCENE
                           else "ui.ram_source_preview"))
                want_idle = i18n.T("ui.ram_idle")
                ram_layout = _LayoutRecorder()
                ui.SEQVEL_PT_speedline.draw(
                    types.SimpleNamespace(layout=ram_layout), context)
            finally:
                i18n._LANG_OVERRIDE = _lang
            ram_blob = "\n".join(ram_layout.labels)
            check("面板画出内存预览分区（标题）", want_section in ram_blob,
                  "%r / %r" % (want_section, ram_blob[-120:]))
            check("面板报出本次范围与来源", want_range in ram_blob,
                  "%r / %r" % (want_range, [t for t in ram_layout.labels
                                            if "range" in t.lower()]))
            check("没构建过就说「未构建」", want_idle in ram_blob,
                  "%r / %r" % (want_idle, ram_blob[-120:]))

            strip.select = True
            editor.active_strip = strip
            diag = carrier.write_curve(strip, carrier.STORE.get_or_create(strip))
            check("曲线落地为 Speed Control 条带",
                  bool(diag["carrier"]) and not diag["errors"], diag)

            landed = carrier.carrier_of(strip)
            check("承载条带不开混合帧（帧精度策略）",
                  landed is not None and landed.use_frame_interpolate is False,
                  getattr(landed, "use_frame_interpolate", None))

            # ------------------------------------------------------------------
            # 上游缺陷（源图缓存吞子帧）的补救出口：一键开关，点了才改、可再点恢复
            # ------------------------------------------------------------------
            check("Raw 缓存开关操作符已注册",
                  hasattr(bpy.types, "SEQVEL_OT_toggle_raw_cache"))
            editor.use_cache_raw = False
            with bpy.context.temp_override(window=bpy.context.window, screen=screen,
                                           area=area, region=region):
                bpy.ops.seqvel.toggle_raw_cache()
            check("点一下按钮即打开本场景 Raw 缓存", editor.use_cache_raw is True)
            cache_layout = _LayoutRecorder()
            ui.SEQVEL_PT_speedline.draw(
                types.SimpleNamespace(layout=cache_layout), context)
            check("取整选帧素材不弹「无级受限」（缓存开着也一样）",
                  not any("无级受限" in text for text in cache_layout.labels),
                  [t for t in cache_layout.labels if "Raw" in t or "无级" in t])
            bpy.ops.seqvel.toggle_raw_cache()
            check("再点一下恢复原状", editor.use_cache_raw is False)

            # ------------------------------------------------------------------
            # 编辑会话：真实 area / region / view2d
            #
            # 只有 window_manager 用替身（modal_handler_add 要求真 Operator 实例）；
            # area / region / view2d 全是真的，这才是要验证的东西。
            # ------------------------------------------------------------------
            session_context = types.SimpleNamespace(
                scene=real.scene, region=region, area=area,
                window_manager=types.SimpleNamespace(modal_handler_add=lambda op: None))
            view2d = region.view2d
            base = float(strip.left_handle)
            channel = float(strip.channel)
            duration = float(strip.right_handle - strip.left_handle)

            def pixel(rel_frame, speed):
                return geometry.point_xy(view2d, base, channel, rel_frame, speed)

            def gui_event(kind, local, value="PRESS"):
                """真机风格事件：区域局部坐标 + 窗口绝对坐标（= 区域原点 + 局部）。"""
                return _Event(kind, local[0], local[1], value=value,
                              window=(region.x + local[0], region.y + local[1]))

            strip.select = True
            editor.active_strip = strip
            click = pixel(50.0, 1.0)
            target = pixel(50.0, 1.5)
            check("真实 view2d 像素可换算", click is not None, view2d)

            # ------------------------------------------------------------------
            # 区域定位（本次修复的核心）
            # ------------------------------------------------------------------
            shadow = carrier.STORE.get_or_create(strip)
            on_line = geometry.hit_test(view2d, base, channel, duration, shadow, click)
            check("时间轴 view2d 上能命中线上",
                  on_line.kind == geometry.HIT_SEGMENT, on_line)

            # 票 03：右键分流进了一步——命中线的某一段 → 段曲线类型菜单
            # （ADR-0005 修订 R5）。这里只复核"分流走对了、目标记对了"，
            # 菜单本身用替身挡掉（真弹窗会留在窗口上干扰后续检查）。
            opened = []
            original_open = interaction._open_menu
            interaction._open_menu = lambda ctx, name, label: (opened.append(name), True)[1]
            try:
                menu_stub = _OperatorStub()
                menu_result = interaction.SEQVEL_OT_point_menu._start(
                    menu_stub, session_context, gui_event("RIGHTMOUSE", click))
                segment_target = interaction._peek_segment_target()
            finally:
                interaction._open_menu = original_open
                interaction._SEGMENT_TARGET = None
            check("右键线上的某一段 → 记录段目标 + 弹段菜单",
                  menu_result == {"FINISHED"} and opened == ["SEQVEL_MT_segment_menu"]
                  and segment_target is not None and segment_target[1] == 0,
                  "%s / %s / %s" % (menu_result, opened, segment_target))

            others = [r for r in area.regions
                      if r.type in NON_TIMELINE_REGIONS
                      and r.width > 2 and r.height > 2]
            check("本机确实存在'有 view2d 但不是时间轴'的区域",
                  len(others) > 0, [r.type for r in others])
            for other in others:
                mapped = other.view2d.region_to_view(click[0], click[1])
                same_space = mapped is not None and abs(mapped[1] - channel) < 0.5
                check("%s 区域的 view2d 坐标系与时间轴不同（bug 的前提）" % other.type,
                      not same_space, "channel=%r" % (mapped,))
                found = regions.timeline_region(
                    types.SimpleNamespace(area=area, region=other))
                ok = (found is not None and found.type == "WINDOW"
                      and found.x == region.x and found.y == region.y
                      and found.width == region.width)
                check("从 %s 区域出发也定位到时间轴区域" % other.type, ok,
                      "找到 %s" % (getattr(found, "type", None),))

            # 面板按钮已移除（ADR-0005）：execute（无 event）必须礼貌拒绝，
            # 不得假装成功、更不得崩。
            ui_region = by_type.get("UI")
            check("侧栏已展开（UI 区域可用真实对象复核）",
                  ui_region is not None and ui_region.width > 2,
                  "UI 宽度=%s" % (getattr(ui_region, "width", None),))
            if ui_region is not None and ui_region.width > 2:
                panel_context = types.SimpleNamespace(
                    scene=real.scene, region=ui_region, area=area,
                    window_manager=types.SimpleNamespace(
                        modal_handler_add=lambda op: None))
                check("面板按钮路径的 context.region 是 UI 区域（不是时间轴）",
                      panel_context.region.type == "UI", panel_context.region.type)
                panel_op = _OperatorStub()
                result = panel_op.execute(panel_context)
                check("侧栏区域下 execute（无 event）礼貌拒绝并说明原因",
                      result == {"CANCELLED"} and len(panel_op.reports) == 1,
                      "%s / reports=%r" % (result, panel_op.reports))
                check("拒绝不进入编辑态", status.STATE["editing"] == "",
                      status.STATE["editing"])

            # 1) execute（无 event）：礼貌拒绝（F3 / 脚本路径同理）
            button_op = _OperatorStub()
            result = button_op.execute(session_context)
            check("execute（无 event）礼貌拒绝并说明原因",
                  result == {"CANCELLED"} and len(button_op.reports) == 1,
                  "%s / reports=%r" % (result, button_op.reports))
            button_op._cleanup(session_context)

            # 2) invoke(event=None)：不崩、不吃点击（穿透）
            none_op = _OperatorStub()
            try:
                result = none_op.invoke(session_context, None)
                ok = result == {"PASS_THROUGH"} and none_op.reports == []
            except Exception as exc:
                ok, result = False, "%s: %s" % (type(exc).__name__, exc)
            check("invoke(event=None) 不崩且穿透", ok, result)

            # 3) 直接操纵：按在线上 → 拖动 → 松手落盘
            #    复位模型，否则下面按 1.0x 算出来的像素根本不在线上。
            carrier.STORE.forget(strip)
            carrier.delete_carriers(strip)
            carrier.STORE.invalidate()
            strip.select = True
            editor.active_strip = strip
            click = pixel(50.0, 1.0)
            target = pixel(50.0, 1.5)

            tool_op = _OperatorStub()
            result = tool_op.invoke(session_context, gui_event("LEFTMOUSE", click))
            check("按在速度线上进入模态（真实 view2d）",
                  result == {"RUNNING_MODAL"}
                  and getattr(tool_op, "mode", None) == "SEGMENT",
                  "%s / mode=%r" % (result, getattr(tool_op, "mode", None)))

            tool_op.modal(session_context, gui_event("MOUSEMOVE", target,
                                                     value="NOTHING"))
            speeds = [round(speed, 2) for _f, speed in tool_op.curve.points]
            # 真实 view2d 的像素是整数网格，speed 会差一点点（亚像素，肉眼不可见）
            check("拖动后影子模型更新",
                  len(speeds) == 2 and all(abs(s - 1.5) < 0.05 for s in speeds), speeds)

            tool_op.modal(session_context, gui_event("LEFTMOUSE", target,
                                                     value="RELEASE"))
            stored = strip.get(store.PROP_KEY)
            check("松手后曲线写进条带自定义属性", stored is not None, stored)
            found = carrier.carrier_of(strip)
            curve = carrier.speed_curve(found) if found is not None else None
            keys = [(kp.co[0], round(kp.co[1], 2)) for kp in curve.keyframe_points] if curve else []
            check("原生 speed_factor 关键帧落地",
                  len(keys) == 2 and all(abs(k[1] - 1.5) < 0.05 for k in keys), keys)

            tool_op.modal(session_context, _Event("ESC"))
            check("Esc 退出后回到空闲", not status.STATE["editing"], status.STATE["editing"])

            status.clear_error()

        seqvel.unregister()
        say("=== 完成：%s ===" % ("全部通过" if not _FAILURES
                                else "失败 %d 项" % len(_FAILURES)))
    except Exception:
        _FAILURES.append("exception")
        say("EXCEPTION")
        for line in traceback.format_exc().splitlines():
            say(line)
    _quitting()
    return None


bpy.app.timers.register(run, first_interval=1.5)
