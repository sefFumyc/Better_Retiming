# -*- coding: utf-8 -*-
"""测试夹具。

- ``FakeView2D``：纯 Python 的 ``region.view2d`` 替身，供几何/命中检测单测使用；
- bpy 相关夹具（场景、条带、测试媒体）延后导入 bpy，纯 Python 下也能 import 本模块。
"""

import importlib.util
import math
import os
import struct
import tempfile
import types
import wave

HAVE_BPY = importlib.util.find_spec("bpy") is not None

MEDIA_DIR = os.path.join(tempfile.gettempdir(), "seqvel_tests")


class FakeView2D(object):
    """模拟 VSE 时间轴的 ``region.view2d``。

    view 坐标：x = 时间轴帧，y = 通道；像素 = 线性映射。
    默认 1 帧 10 像素、1 通道 60 像素、通道 0 落在区域 y=0。
    """

    def __init__(self, frame_origin=1.0, channel_origin=0.0,
                 x_scale=10.0, y_scale=60.0, region_origin=(0.0, 0.0),
                 clip_rect=None):
        self.frame_origin = float(frame_origin)
        self.channel_origin = float(channel_origin)
        self.x_scale = float(x_scale)
        self.y_scale = float(y_scale)
        self.region_origin = (float(region_origin[0]), float(region_origin[1]))
        self.broken = False
        self.clip_flags = []
        # 裁剪模式（clip=True）的模拟：真机 view2d 在点出视口时返回 None，
        # clip_rect 即"网格 widget 矩形"（不含滚动条）；None = 模拟不支持
        # 裁剪模式的旧式 view2d（只有双参签名，clip 参数收下但忽略）。
        self.clip_rect = clip_rect

    def view_to_region(self, frame, channel, clip=True):
        self.clip_flags.append(clip)
        if self.broken:
            return None
        x = (float(frame) - self.frame_origin) * self.x_scale + self.region_origin[0]
        y = (float(channel) - self.channel_origin) * self.y_scale + self.region_origin[1]
        if clip and self.clip_rect is not None:
            x0, y0, x1, y1 = self.clip_rect
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                return None
        return (x, y)

    def region_to_view(self, x, y):
        if self.broken:
            return None
        frame = self.frame_origin + (float(x) - self.region_origin[0]) / self.x_scale
        channel = self.channel_origin + (float(y) - self.region_origin[1]) / self.y_scale
        return (frame, channel)

    # --- 便捷换算，供测试书写断言 ---
    def pixel_of(self, frame, channel):
        return self.view_to_region(frame, channel)

    def distance(self, mouse, a, b):
        ax, ay = a
        bx, by = b
        vx, vy = bx - ax, by - ay
        wx, wy = mouse[0] - ax, mouse[1] - ay
        length_sq = vx * vx + vy * vy
        if length_sq <= 0.0:
            return math.hypot(mouse[0] - ax, mouse[1] - ay)
        ratio = max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
        return math.hypot(mouse[0] - (ax + ratio * vx), mouse[1] - (ay + ratio * vy))


class LegacyView2D(object):
    """只有旧版双参 ``view_to_region`` 的 view2d 替身（不接受 clip 参数）。

    模拟两类真实场景：极老 Blender 的 RNA、没实现 clip 模式的假对象——
    凡是依赖 clip 探测的逻辑遇到它都必须走兜底路径。
    """

    def view_to_region(self, frame, channel):
        return (frame * 2.0, channel * 3.0)

    def region_to_view(self, x, y):
        return (x / 2.0, y / 3.0)


# --------------------------------------------------------------------------
# bpy 夹具
# --------------------------------------------------------------------------

def reset_scene():
    """回到干净场景（每个用例前调用）。"""
    import bpy

    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    except Exception:
        pass
    scene = bpy.context.scene
    if scene.sequence_editor is None:
        scene.sequence_editor_create()
    return scene


def scene_editor(scene=None):
    import bpy

    scene = scene or bpy.context.scene
    if scene.sequence_editor is None:
        scene.sequence_editor_create()
    return scene.sequence_editor


def _ensure_media_dir():
    if not os.path.isdir(MEDIA_DIR):
        os.makedirs(MEDIA_DIR)
    return MEDIA_DIR


def media_png():
    """生成一张小的测试 PNG（图像条带用）。"""
    import bpy

    path = os.path.join(_ensure_media_dir(), "seqvel_test.png")
    if not os.path.exists(path):
        image = bpy.data.images.new("seqvel_test_media", 16, 16)
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
    return path


def media_wav():
    """生成一小段静音 WAV（音频条带用）；失败返回 None。"""
    path = os.path.join(_ensure_media_dir(), "seqvel_test.wav")
    if os.path.exists(path):
        return path
    try:
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(struct.pack("<%dh" % 4410, *([0] * 4410)))
    except Exception:
        return None
    return path


def make_strip(length=120, name="target", channel=1, frame_start=1):
    """建一条指定长度的图像条带。"""
    editor = scene_editor()
    strip = editor.strips.new_image(name=name, filepath=media_png(),
                                    channel=channel, frame_start=frame_start)
    strip.right_handle = strip.left_handle + int(length)
    return strip


def make_scene_strip(name="scene_target", channel=1, frame_start=1, length=120):
    """建一条场景条带（新开一个源场景）。

    场景条带是唯一走**子帧渲染**的源类型（其余类型渲染前取整选帧），
    所以取帧策略相关的断言都拿它当被试。
    """
    import bpy

    editor = scene_editor()
    source = bpy.data.scenes.new("seqvel_src_%s" % name)
    strip = editor.strips.new_scene(name=name, scene=source, channel=channel,
                                    frame_start=frame_start)
    strip.right_handle = strip.left_handle + int(length)
    return strip


def make_sound_strip(name="audio", channel=1, frame_start=1):
    """建一条音频条带；媒体不可用返回 None。"""
    path = media_wav()
    if path is None:
        return None
    editor = scene_editor()
    try:
        return editor.strips.new_sound(name=name, filepath=path,
                                       channel=channel, frame_start=frame_start)
    except Exception:
        return None


def strip_span(strip):
    return float(strip.right_handle - strip.left_handle)


# --------------------------------------------------------------------------
# 真机风格的区域替身
# --------------------------------------------------------------------------
#
# 为什么需要这些：原来的夹具把 region 写成 ``SimpleNamespace(view2d=...)``，
# 既没有 ``type`` 也没有 ``area.regions`` —— 于是"操作符拿到的是哪个区域的
# view2d"这件事**根本无法表达**。结果 156 个用例全绿，而真机上"进了编辑态
# 却怎么点都没反应"。下面的替身把真机事实搬进来（数值取自 5.2.1 实测）：
#
# 1. ``region.view2d`` 在**所有**区域类型上都不是 None：HEADER / UI / TOOLS /
#    CHANNELS / FOOTER / PREVIEW 全都有，连 1x1 的折叠区域也有；
# 2. 但只有 WINDOW（时间轴）那个的坐标系是"帧 / 通道"；
# 3. ``region.x/y`` 与 ``event.mouse_x/y`` 同为**窗口绝对**坐标。

#: 时间轴（WINDOW）区域在窗口里的位置与尺寸（实测值）
TIMELINE_ORIGIN = (218, 95)
TIMELINE_SIZE = (1358, 862)
#: 工具栏（TOOLS）区域
TOOLS_ORIGIN = (2, 95)
TOOLS_SIZE = (56, 862)
#: 侧栏（UI）区域 —— 面板按钮 / 面板上的 Alt+S 拿到的就是它
UI_ORIGIN = (1575, 95)
UI_SIZE = (300, 862)


class DeadView2D(object):
    """非时间轴区域的 view2d：**存在**，但换算毫无意义。

    真机实测：UI 区域的 ``region_to_view`` 返回 ``(nan, nan)``；
    TOOLS / CHANNELS 返回有限但离谱的值（y ≈ -266）。这里取前者——只要代码
    用了它，命中检测必然 ``HIT_NONE``，所以"用错区域"这件事能被测试抓到。
    """

    def view_to_region(self, frame, channel, clip=True):
        return (float("nan"), float("nan"))

    def region_to_view(self, x, y):
        return (float("nan"), float("nan"))


class FakeRegion(object):
    def __init__(self, kind, x=0, y=0, width=100, height=100, view2d=None):
        self.type = kind
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.view2d = view2d


class FakeArea(object):
    def __init__(self, kind="SEQUENCE_EDITOR", x=2, y=23, width=1574, height=888):
        self.type = kind
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.regions = []
        self.header_text = None
        self.redraws = 0

    def add(self, region):
        self.regions.append(region)
        return region

    def header_text_set(self, text):
        self.header_text = text

    def tag_redraw(self):
        self.redraws += 1


def make_editor_context(scene, view2d, active="WINDOW", window_manager=None):
    """搭一个"像真机"的 VSE 上下文；返回 ``(context, regions)``。

    ``regions`` 里有三个区域，其中只有 WINDOW 的 view2d 是有效的时间轴坐标系；
    ``context.region`` 由 ``active`` 指定，用来复现三条入口的**真实**取值：

    ====================  ================
    入口                   context.region
    ====================  ================
    面板「编辑」按钮        ``UI``（侧栏）
    ``Alt+S``（鼠标在面板） ``UI``
    工具栏「速度线」按钮    ``TOOLS``
    在时间轴上直接点击      ``WINDOW``
    ====================  ================

    只有最后一种是对的——所以 ``active`` 不是 ``WINDOW`` 时**断言必须照样成立**。
    """
    area = FakeArea()
    regions = {}
    for name, origin, size in (("WINDOW", TIMELINE_ORIGIN, TIMELINE_SIZE),
                               ("TOOLS", TOOLS_ORIGIN, TOOLS_SIZE),
                               ("UI", UI_ORIGIN, UI_SIZE)):
        regions[name] = area.add(FakeRegion(
            name, x=origin[0], y=origin[1], width=size[0], height=size[1],
            view2d=view2d if name == "WINDOW" else DeadView2D()))
    context = types.SimpleNamespace(
        scene=scene,
        area=area,
        region=regions[active],
        screen=types.SimpleNamespace(areas=[area]),
        window_manager=window_manager or types.SimpleNamespace(
            modal_handler_add=lambda op: None),
    )
    return context, regions


def make_event(kind, value="PRESS", local=None, window=None):
    """真机风格的事件。

    ``local``  → ``mouse_region_x/y``：事件被派发到的**那个**区域的局部坐标；
    ``window`` → ``mouse_x/y``：窗口绝对坐标（真机上与 ``region.x/y`` 同源）。

    两者都给时，代码应当以窗口坐标为准：真机上 ``mouse_region_*`` 可能属于
    另一个区域（事件在时间轴里、而操作符的 ``context.region`` 是侧栏）。
    """
    event = types.SimpleNamespace(type=kind, value=value)
    if local is not None:
        event.mouse_region_x = float(local[0])
        event.mouse_region_y = float(local[1])
    if window is not None:
        event.mouse_x = float(window[0])
        event.mouse_y = float(window[1])
    return event

