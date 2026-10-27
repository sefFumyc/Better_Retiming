# -*- coding: utf-8 -*-
"""定位 VSE 时间轴区域（``WINDOW``）——**不要**相信 ``context.region``。

真机（Blender 5.2.1，真实 GUI 窗口）实测三点：

1. ``region.view2d`` 在**所有**区域类型上都不是 ``None``：``HEADER`` / ``UI`` /
   ``TOOLS`` / ``CHANNELS`` / ``FOOTER`` / ``PREVIEW`` 全都返回一个 View2D，
   连 1x1 的折叠区域也有。所以"拿不到 view2d 就放弃"这类防御**永远不会触发**，
   换来的是一个**坐标系不符**的 view2d。
2. 同一个鼠标像素、同一条曲线，只换 view2d 来源（实测数据）：

   ==============  ===================  =========================
   view2d 来源      命中结果             region_to_view(鼠标)
   ==============  ===================  =========================
   WINDOW（时间轴）  Hit(SEGMENT, 0) ✓    (94.26, 5.50)
   UI（侧栏面板）     Hit(NONE)           (nan, nan)
   TOOLS（工具栏）    Hit(NONE)           (46.84, -266.35)
   CHANNELS          Hit(NONE)           (46.29, -266.35)
   ==============  ===================  =========================

3. 三条入口的 ``context.region`` 都不是时间轴：面板「编辑」按钮走侧栏 ``UI``
   区域；``Alt+S`` 在鼠标停在面板上时同理；工具栏「速度线」按钮是 ``TOOLS``。

合起来正好是用户看到的现象：**速度线画得出来**（绘制走
``SpaceSequenceEditor`` 的 ``WINDOW/POST_PIXEL`` 回调，那里的 region 必然正确），
**但怎么点都没反应**（命中的 view2d 是错的，``hit_test`` 永远 ``HIT_NONE``）。

因此凡是"把鼠标换算成帧/通道"或"画到时间轴上"的地方，都从这里取 view2d。

模块本身零 bpy（只用 getattr），因此纯 Python 下也能直接单测。
"""

from . import i18n

#: 时间轴区域的类型名
TIMELINE_REGION = "WINDOW"

#: 区域尺寸下限：1x1 的折叠区域没有可用的 view2d 数据
MIN_REGION_SIZE = 2

#: 视频序列编辑器的区域类型名
EDITOR_AREA = "SEQUENCE_EDITOR"

#: 空间类型名（``context.space_data.type``）
EDITOR_SPACE = "SEQUENCE_EDITOR"


def displayed_scene(context):
    """VSE 实际**显示**的场景：5.x「序列场景」优先，退回活动场景。

    Blender 5.0 起，VSE 时间线**不再显示窗口的活动场景**，而是显示工作区上
    单独指定的「序列场景」（Sequencer Scene：时间线头部的场景选择器 /
    ``workspace.sequencer_scene``），并且 ``context.sequencer_scene`` 直接
    暴露它。活动场景与序列场景可以各是各的——真机故障（5.blend）：活动场景
    「Scene」里只有一条 19468 帧的 mp3，序列场景「场景」才是编辑对象；插件
    若读 ``context.scene`` 就会看错场景：面板永远悬停在 mp3 上（"音频条带
    不能编辑"）、速度线画不出、点条带没反应。
    注意 ``SpaceSequenceEditor`` **没有** ``scene`` 属性（5.2.1 RNA 实测），
    "钉在空间上的场景"这种猜测的取法不存在。扫描 / 绘制 / 设置读写一律以
    本函数为准。
    """
    shown = getattr(context, "sequencer_scene", None)
    if shown is not None:
        return shown
    space = getattr(context, "space_data", None)
    if getattr(space, "type", "") == EDITOR_SPACE:
        workspace = getattr(context, "workspace", None)
        shown = getattr(workspace, "sequencer_scene", None)
        if shown is not None:
            return shown
    return getattr(context, "scene", None)


def timeline_area(context):
    """当前上下文里的 VSE 区域；找不到返回 None。

    ``context.area`` 不是 VSE 时回退到 ``context.screen``：从 F3 搜索、
    脚本或别的编辑器里调用时，``context.area`` 可能是信息面板之类。
    """
    area = getattr(context, "area", None)
    if getattr(area, "type", "") == EDITOR_AREA:
        return area
    screen = getattr(context, "screen", None)
    for candidate in getattr(screen, "areas", ()) or ():
        if getattr(candidate, "type", "") == EDITOR_AREA:
            return candidate
    return None


def is_timeline(region):
    """这个区域是不是可用的时间轴区域。"""
    return (region is not None
            and getattr(region, "type", "") == TIMELINE_REGION
            and getattr(region, "width", 0) >= MIN_REGION_SIZE
            and getattr(region, "height", 0) >= MIN_REGION_SIZE)


def timeline_region(context):
    """VSE 时间轴（``WINDOW``）区域；找不到返回 None。

    ``context.region`` 恰好就是时间轴时直接用它（省一次遍历，也保留 Blender
    自己的区域引用）；否则在 ``context.area.regions`` 里按类型找。**不要**退回
    ``context.region``——那是 bug 本身：侧栏/工具栏区域的 view2d 存在但没用。
    """
    region = getattr(context, "region", None)
    if is_timeline(region):
        return region
    area = timeline_area(context)
    for candidate in getattr(area, "regions", ()) or ():
        if is_timeline(candidate):
            return candidate
    return None


def view2d(context):
    """时间轴的 ``view2d``；拿不到返回 None。"""
    return getattr(timeline_region(context), "view2d", None)


def region_label(context):
    """给面板看的一行区域说明（找不到时说明原因；运行层文案，ADR-0009）。"""
    region = timeline_region(context)
    if region is None:
        area = getattr(context, "area", None)
        if area is None:
            return i18n.T("regions.no_area")
        if getattr(area, "type", "") != EDITOR_AREA:
            return i18n.T("regions.wrong_area", getattr(area, "type", ""))
        return i18n.T("regions.no_timeline")
    return i18n.T("regions.timeline", region.width, region.height)


def mouse(event, context):
    """事件里的鼠标位置，换算成**时间轴区域**坐标；没有事件返回 None。

    为什么不能直接用 ``event.mouse_region_x/y``：那是**事件被交给的那个区域**
    的局部坐标。当 ``context.region`` 是侧栏/工具栏（面板按钮、侧栏里的 Alt+S）
    时两者不是一回事 —— 拿 UI 区域的局部坐标去比时间轴的像素，必然落空。

    ``event.mouse_x/y`` 与 ``region.x/y`` 同为**窗口绝对**坐标（实测：HEADER 的
    region.y=957 落在 area 的 y=95..983 内；若是区域相对坐标，上限只有 888），
    相减即得时间轴区域坐标。缺少 ``mouse_x/y`` 时（测试替身、宿主构造的事件）
    退回 ``mouse_region_*``。
    """
    if event is None:
        return None
    region = timeline_region(context)
    window_x = getattr(event, "mouse_x", None)
    window_y = getattr(event, "mouse_y", None)
    if region is not None and window_x is not None and window_y is not None:
        try:
            return (float(window_x) - float(region.x),
                    float(window_y) - float(region.y))
        except Exception:
            pass
    region_x = getattr(event, "mouse_region_x", None)
    region_y = getattr(event, "mouse_region_y", None)
    if region_x is None or region_y is None:
        return None
    return (float(region_x), float(region_y))
