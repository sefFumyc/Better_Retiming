# -*- coding: utf-8 -*-
"""编辑交互的纯逻辑（零 bpy）——直接操纵（ADR-0005）拆出来的两块状态。

不存在常驻编辑会话后，有两块状态必须活在 operator 实例之外：

- **双击检测**：一次双击 = 两个独立的短 modal（按下→松手各一次 operator
  运行），检测状态跨运行存活，所以放在模块级持有；
- **右键点目标**：右键按在速度点上时记下"哪个条带的第几个点"，随后的
  删除（点菜单项点击）凭它定位。它必须带**新鲜期**：菜单关掉
  很久之后按下的 D 绝不能删掉一个看不见的旧点。

本模块只吃可序列化的简单值，方便在无 Blender 环境下单测。
"""

import time

#: 手动双击检测参数（不依赖 Blender DOUBLE_CLICK 事件；沿用原型 v0.4.2 定稿）
DOUBLE_CLICK_SECONDS = 0.45
DOUBLE_CLICK_PIXELS = 12.0

#: 右键点目标的默认新鲜期（秒）。覆盖"菜单开着慢慢想"的合理时长，
#: 又能在菜单被丢弃后不久封死误删窗口。
POINT_TARGET_TTL = 15.0


class DoubleClick(object):
    """跨 operator 运行的双击检测状态机。

    ``click(key, px, now)``：一次松手（未发生拖动）上报一次。
    ``key`` 用来隔离不同条带（在 A 条带点了一下、B 条带点一下，不算双击）；
    ``px`` 为 ``None``（无鼠标坐标的路径）时忽略并清状态。
    返回 ``True`` 表示构成双击（内部随即重置：三连点 = 双击 + 新的第一次）。
    """

    def __init__(self, seconds=DOUBLE_CLICK_SECONDS, pixels=DOUBLE_CLICK_PIXELS,
                 clock=time.monotonic):
        self._seconds = seconds
        self._pixels = pixels
        self._clock = clock
        self._last = None  # (key, (x, y), now)

    def click(self, key, px, now=None):
        if px is None:
            self.forget()
            return False
        now = self._clock() if now is None else now
        last = self._last
        self._last = (key, px, now)
        if last is None:
            return False
        last_key, last_px, last_at = last
        if last_key != key:
            return False
        if (now - last_at) >= self._seconds:
            return False
        dx = px[0] - last_px[0]
        dy = px[1] - last_px[1]
        if (dx * dx + dy * dy) ** 0.5 >= self._pixels:
            return False
        self.forget()
        return True

    def forget(self):
        """清状态：发生拖动、换条带、或任何"不是连击"的打断后调用。"""
        self._last = None


class PointTarget(object):
    """右键选中的待删除点（条带名 + 点序号），带新鲜期。

    ``use(now)``：在新鲜期内返回 ``(条带名, 点序号)``，过期/缺序号返回
    ``None``（调用方必须拒绝动作并说明原因，绝不静默）。
    """

    def __init__(self, strip_name, index, made_at, ttl=POINT_TARGET_TTL):
        self.strip_name = strip_name
        self.index = index
        self._made_at = made_at
        self._ttl = ttl

    def use(self, now):
        if self.index is None:
            return None
        if self._ttl <= 0.0 or (now - self._made_at) > self._ttl:
            return None
        return (self.strip_name, self.index)
