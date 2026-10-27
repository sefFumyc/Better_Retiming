# -*- coding: utf-8 -*-
"""速度线绘制 overlay（VSE 的 WINDOW / POST_PIXEL 绘制回调）。

显示内容（对照原型 v0.4.2 定稿样式）：

- 黄色速度线（末点左移时右侧补一条水平延长线，铺满素材末尾）；
- 白色半透明 1.0x 基线；
- 橙色方块速度点；
- 速度百分比 HUD：勾选时全部点常显；未勾选时平日没有数字，只在**拖动中临时**
  显示本次正在改变的那些点（R10，见 ``visible_hud_indices``）。

容错策略：着色器名、线宽、坐标越界、单条带坏数据都不允许拖垮整体绘制；
最近一次绘制错误与诊断计数写到 ``status``，面板可读。
"""

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import geometry, i18n, regions, rules, status, ui
from .store import STORE, strip_channel, strip_duration, strip_key

COLOR_LINE = (1.0, 0.85, 0.15, 1.0)
COLOR_BASELINE = (1.0, 1.0, 1.0, 0.22)
COLOR_POINT = (1.0, 0.45, 0.2, 1.0)
COLOR_HUD = (1.0, 1.0, 1.0, 0.9)

POINT_SIZE = 7.0
HUD_FONT_SIZE = 12
HUD_FONT_ID = 0
SHADER_NAMES = ("2D_UNIFORM_COLOR", "UNIFORM_COLOR")

_handle = None
_shader = None
_last_error = None


# --------------------------------------------------------------------------
# 底层绘制
# --------------------------------------------------------------------------

def _get_shader():
    global _shader
    if _shader is not None:
        return _shader
    for name in SHADER_NAMES:
        try:
            _shader = gpu.shader.from_builtin(name)
            return _shader
        except Exception:
            continue
    return None


def _draw_polyline(shader, points, color, width=2.0):
    if len(points) < 2:
        return
    shader.uniform_float("color", color)
    try:
        gpu.state.line_width_set(width)
    except Exception:
        pass
    batch_for_shader(shader, "LINE_STRIP", {"pos": [(p[0], p[1]) for p in points]}).draw(shader)


def _draw_square(shader, center, size, color):
    half = size / 2.0
    x, y = center
    positions = [
        (x - half, y - half), (x + half, y - half), (x + half, y + half),
        (x - half, y - half), (x + half, y + half), (x - half, y + half),
    ]
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRIS", {"pos": positions}).draw(shader)


def _draw_hud(text, position):
    blf.color(HUD_FONT_ID, *COLOR_HUD)
    try:
        blf.size(HUD_FONT_ID, HUD_FONT_SIZE)
    except TypeError:  # 旧签名带 dpi
        blf.size(HUD_FONT_ID, HUD_FONT_SIZE, 72)
    blf.position(HUD_FONT_ID, position[0] + 8.0, position[1] + 5.0, 0)
    blf.draw(HUD_FONT_ID, text)


# --------------------------------------------------------------------------
# 单条带绘制
# --------------------------------------------------------------------------

def visible_hud_indices(strip, curve, show_all, target):
    """[修订 R10] 本次绘制该显示哪些点的百分比。

    - 勾选「显示速度百分比」→ **全部**（此时临时机制被完全覆盖，不做第三态）；
    - 未勾选 + 没在拖这条线 → **空集**（平日没有数字）；
    - 未勾选 + 正在拖这条线 → **本次正在改变的那些点**（点拖 1 个 / 段拖 2 个
      端点 / 尾段拖末点 1 个），越界下标直接丢弃。

    ``target`` 是 ``status.drag_target()``：``(strip_key, (下标, ...))`` 或 None。
    判定按条带键（场景名 + 条带名）比对——同屏别的可见条带照旧不显示。
    """
    count = len(curve.points)
    if show_all:
        return set(range(count))
    if not target:
        return set()
    key, indices = target
    if key != strip_key(strip):
        return set()
    return {index for index in indices if 0 <= index < count}


def draw_strip(view2d, strip, curve, shader, hud_indices=None):
    """把一条条带的曲线画到给定 view2d 上；view2d 不可用抛 RuntimeError。

    ``view2d`` 由调用方经 ``regions`` 取（**必须是时间轴区域的那个**）：
    参数写成 view2d 而不是 region，就是为了让"用错区域的 view2d"不可能悄悄发生
    —— 侧栏区域的 view2d 也存在，但坐标系不同，画出来的位置毫无意义。

    ``hud_indices``：要画百分比 HUD 的**点下标集合**；``None`` 表示全画。
    判断刻意留在调用方（``_draw_all`` + ``visible_hud_indices``）——它要读场景
    设置与拖动状态（R10），本函数只执行。
    """
    if view2d is None:
        raise RuntimeError("没有时间轴 view2d")
    base_frame = float(strip.left_handle)
    channel = strip_channel(strip)
    duration = strip_duration(strip)

    # 画线吃采样折线（缓动段才有形状）；方块/尾线仍按**模型点**定位。
    # 采样步长与命中检测同源（geometry.sample_step），"看得见的弯"就是"抓得到的弯"。
    samples, _owners = curve.sampled_points(geometry.sample_step(view2d))
    pixel_points = geometry.curve_xy(view2d, base_frame, channel, curve.points)
    line_points = geometry.curve_xy(view2d, base_frame, channel, samples)
    if pixel_points is None or line_points is None:
        return False

    _draw_polyline(shader, line_points, COLOR_LINE, width=2.0)

    tail_end = curve.tail_extension_end(duration)
    if tail_end is not None:
        end_xy = geometry.tail_end_xy(view2d, base_frame, channel, tail_end, curve.points[-1][1])
        if end_xy is not None:
            _draw_polyline(shader, [line_points[-1], end_xy], COLOR_LINE, width=2.0)

    baseline = geometry.baseline_xy(view2d, base_frame, channel, duration)
    if baseline is not None:
        _draw_polyline(shader, baseline, COLOR_BASELINE, width=1.0)

    for index, (rel_frame, speed) in enumerate(curve.points):
        center = pixel_points[index]
        _draw_square(shader, center, POINT_SIZE, COLOR_POINT)
        if hud_indices is None or index in hud_indices:
            try:
                _draw_hud("%d%%" % round(speed * 100.0), center)
            except Exception:
                pass
    return True


def visible_strips(context):
    """可见条带集合——**可见 = 可抓**（ADR-0005），判定统一走纯规则模块。

    建过速度曲线的条带常开显示；没建过的只在选中/活动时显示默认线。
    命中检测（``interaction``）用同一个 ``rules.visible_strips``，
    两个集合因此永不脱节：不存在"看不见却能抓住的线"。
    """
    scene = regions.displayed_scene(context)  # VSE 显示的场景，不是活动场景
    editor = getattr(scene, "sequence_editor", None)
    if editor is None:
        return []
    return rules.visible_strips(getattr(editor, "strips", ()),
                                getattr(editor, "active_strip", None),
                                STORE.has_model)


def _clip_rect(view2d, region):
    """本帧绘制的 GPU 裁剪矩形（区域像素）。

    优先用 view2d 的网格区（``geometry.view2d_mask``，不含滚动条——条带被
    滚动条遮住时速度线也该被遮住）；量不出来时退回整个区域矩形，至少把
    "出界坐标往远处伸"止住。返回 (xmin, ymin, xmax, ymax)。
    """
    width = int(getattr(region, "width", 0) or 0)
    height = int(getattr(region, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    mask = geometry.view2d_mask(view2d, width, height)
    if mask is None:
        return (0, 0, width, height)
    xmin, ymin, xmax, ymax = mask
    # 就近取整：真实 mask 是整数像素矩形，探测误差 < 1e-9 px，取整即还原；
    # 不能用 floor/ceil 外扩——边界上的正负 1e-9 噪声会被放大成 1px 偏差
    return (int(round(xmin)), int(round(ymin)),
            int(round(xmax)), int(round(ymax)))


def _draw_all(context):
    # 绘制走 SpaceSequenceEditor 的 WINDOW/POST_PIXEL 回调，context.region 本
    # 就该是时间轴；仍统一经 regions 取，避免两套取法埋下不一致。
    view2d = regions.view2d(context)
    if view2d is None:
        return i18n.T("ui.draw_no_timeline", regions.region_label(context))
    region = regions.timeline_region(context)
    scene = regions.displayed_scene(context)  # VSE 显示的场景，不是活动场景
    if getattr(scene, "sequence_editor", None) is None:
        return i18n.T("ui.draw_no_editor")

    shader = _get_shader()
    if shader is None:
        return i18n.T("ui.draw_no_shader")
    try:
        gpu.state.blend_set("ALPHA")
    except Exception:
        pass
    # 裁剪到可见区：出界的曲线段/点/HUD 必须跟着时间线一起被遮住，
    # 否则缩放/横移后坐标一路画到区域外（GPU 不会自动裁掉出界图元）。
    clip = _clip_rect(view2d, region)
    _previous_scissor = None
    if clip is not None:
        try:
            _previous_scissor = gpu.state.scissor_get()
        except Exception:
            _previous_scissor = None
        try:
            # 开裁剪并设矩形。5.2.1 的 gpu.state 有 scissor_test_set 开关、
            # 无测试状态的查询（scissor_get 查的是矩形不是开关）：恢复时
            # 能拿到旧矩形就写回，拿不到就重置为区域矩形，测试开关统一关掉
            # ——各绘制方自理状态，不依赖外部。
            gpu.state.scissor_test_set(True)
            gpu.state.scissor_set(clip[0], clip[1],
                                  clip[2] - clip[0], clip[3] - clip[1])
        except Exception:
            pass
    drawn = 0
    transient_points = 0
    show_all = _show_hud(context)
    target = status.drag_target()
    try:
        for strip in visible_strips(context):
            curve = STORE.display_curve(strip)
            try:
                indices = visible_hud_indices(strip, curve, show_all, target)
                if indices and not show_all:
                    # 这次绘制里"因为拖动而临时显形"的点数：写进绘制诊断，
                    # 否则"数字其实没冒出来"在真机上无从证明（全屏文字读不了像素）。
                    transient_points += len(indices)
                if draw_strip(view2d, strip, curve, shader, hud_indices=indices):
                    drawn += 1
            except Exception:
                # 单条带隔离：坏数据只跳过它，不影响其它条带
                continue
    finally:
        try:
            if clip is not None:
                gpu.state.scissor_test_set(False)
                if _previous_scissor is not None:
                    gpu.state.scissor_set(*_previous_scissor)
                else:
                    gpu.state.scissor_set(0, 0, region.width, region.height)
        except Exception:
            pass
        try:
            gpu.state.blend_set("NONE")
        except Exception:
            pass
    return _draw_reason(drawn, transient_points)


def _draw_reason(drawn, transient_points):
    """绘制诊断文案（面板常驻那行「绘制回调 n 次 · …」用它）。

    临时读数**必须记账**（R10）：全屏文字 HUD 读不了像素，不写出来就没人能证伪
    "数字其实没冒出来"。点数 > 0 才带后缀，日常那句保持干净。
    """
    if not drawn:
        return i18n.T("ui.overlays_reason_none")
    if transient_points:
        return i18n.T("ui.draw_transient", drawn, transient_points)
    return i18n.T("ui.draw_ok", drawn)


def _show_hud(context):
    scene = regions.displayed_scene(context)
    settings = getattr(scene, "seqvel", None)
    if settings is None:
        return True
    # v1.5.0 起是动态枚举（双语 tooltip，ADR-0009）：标识 "ON"/"OFF"
    return getattr(settings, "show_hud", "ON") == "ON"


def _callback():
    """绘制回调：自身异常绝不能冒泡到 Blender。"""
    global _last_error
    try:
        reason = _draw_all(bpy.context)
    except Exception:
        import traceback

        error = traceback.format_exc()
        if error != _last_error:  # 只在出现新错误时写控制台，避免每帧刷屏
            print("[seqvel] 绘制错误:\n" + error)
        _last_error = error
        status.set_error(i18n.T("ui.draw_error"))
        status.note_draw(i18n.T("ui.draw_note_exception"))
        return
    _last_error = None
    status.note_draw(reason)


# --------------------------------------------------------------------------
# 刷新（5.2.1 兼容）
# --------------------------------------------------------------------------

def redraw(context):
    """请求重绘当前区域。

    ``Area.tag_redraw`` 在 5.2.1 里仍是 RNA 函数（``hasattr`` 查不到但可调用），
    因此先试它，失败再退回 ``wm.redraw_timer``。
    """
    area = getattr(context, "area", None)
    if area is not None:
        try:
            area.tag_redraw()
            return
        except Exception:
            pass
    try:
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
    except Exception:
        pass


def set_header_text(context, text):
    """在区域标题栏显示提示（兼容缺失）。"""
    area = getattr(context, "area", None)
    if area is None:
        return
    try:
        area.header_text_set(text)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

def register():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceSequenceEditor.draw_handler_add(
            _callback, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _handle, _shader
    if _handle is not None:
        try:
            bpy.types.SpaceSequenceEditor.draw_handler_remove(_handle, "WINDOW")
        except Exception:
            pass
        _handle = None
    _shader = None
