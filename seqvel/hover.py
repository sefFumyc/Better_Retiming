# -*- coding: utf-8 -*-
"""悬停光标（ADR-0005 修订 R3，v1.2.2 重写）：MOUSEMOVE 键位触发 → HAND_POINT。

Blender **没有悬停事件**；想"轮询"鼠标位置也不行——``bpy.types.Window``
**没有 ``mouse_x``/``mouse_y``**（5.2.1 RNA 实测，v1.2.1 的定时器轮询因此
从未生效过）。唯一可靠的鼠标来源是**事件**，而没按键时唯一拿得到事件的
通道就是键位表：把悬停 operator 绑到 Sequencer 的 ``MOUSEMOVE`` 上，
每次移动唤醒一次命中检测。

纪律：

- **旁观者铁律**：对一切事件一律 ``PASS_THROUGH``——悬停是锦上添花，
  绝不许吃掉选择、拖动、播放的任何事件；
- **与绘制/按下共用同一套"可见 = 可抓"集合**（``interaction.locate``），
  悬停暗示"按下去能抓"——暗示了就必须兑现；
- **拖动中（``status.STATE["editing"]`` 非空）不碰光标**：拖动开始前鼠标
  必然悬在线上（不然起不了拖），此刻的 HAND_POINT 一直保持到松手——
  拖动期间本 operator 也不会被唤醒（事件都被拖动 modal 吞了，键位不触发）；
- **``note_hit=False``**：悬停不许冲刷面板"最近一次命中"（那是"按下"的语义）；
- 状态未变不重复设置（``cursor_set`` 每次调用都有事件开销）；
- 任何异常都不许假装成功，但也不许打断事件流：写面板、原样放行。

已知边界（真机验证项）：鼠标离开时间轴区域（去侧栏/预览）后本 operator
不再被唤醒，HAND_POINT 会保留到下次进入时间轴——修这个要全局 MOUSEMOVE
键位，成本大于收益，先接受。
"""

from types import SimpleNamespace

import bpy

from . import geometry, i18n, interaction, status

#: 悬停命中时的光标（指向手指）
CURSOR_ON = "HAND_POINT"

#: 默认光标（恢复用）
CURSOR_OFF = "DEFAULT"

#: 已被我们改成 CURSOR_ON 的窗口（键 = 窗口标识，值 = 窗口对象）。
#: bpy 结构体的可哈希性没有保证（替身 SimpleNamespace 也不可哈希），
#: 一律按 ``as_pointer()``（替身退回 ``id()``）做键。
_set_windows = {}

#: [诊断 v1.2.4→R6] 最近一次 mousemove 的选择/激活快照（结构化 dict）。
#: 按下即激活的翻转发生在键位派发**之前**——invoke 里抓快照必然抓到翻转
#: 后的状态；只有"按下前最后一次悬停"才拿得到按下前的样子。拖动恢复
#: （interaction._restore_selection）以这份快照为基准。
last_sel_data = None


def _key(win):
    try:
        return win.as_pointer()
    except AttributeError:
        return id(win)


def register():
    try:
        bpy.utils.register_class(SEQVEL_OT_hover_cursor)
    except Exception as exc:
        status.set_error(i18n.T("hover.register_failed", exc))


def unregister():
    _restore_all()
    try:
        bpy.utils.unregister_class(SEQVEL_OT_hover_cursor)
    except Exception:
        pass


def _restore_all():
    """把所有被动过的窗口光标恢复成默认（插件卸载时）。"""
    for win in list(_set_windows.values()):
        _set_cursor(win, CURSOR_OFF)
    _set_windows.clear()


def _set_cursor(win, name):
    try:
        win.cursor_set(name)
    except Exception as exc:
        status.set_error(i18n.T("hover.cursor_failed", name, exc))


def _decide(context, mouse_x, mouse_y):
    """这个鼠标位置此刻是否悬在可见的速度线上（不写面板命中诊断）。"""
    event = SimpleNamespace(mouse_x=mouse_x, mouse_y=mouse_y)
    _strip, hit = interaction.locate(context, event, note_hit=False)
    return hit.kind != geometry.HIT_NONE


class SEQVEL_OT_hover_cursor(bpy.types.Operator):
    """悬停光标切换：MOUSEMOVE 键位唤醒，命中→HAND_POINT，离开→DEFAULT。

    键位表每来一次 MOUSEMOVE 就走一遍 ``invoke``；它只读、只改光标，
    然后把事件原样还回去。没有 modal、没有定时器、没有全局状态之外的开销。
    """

    bl_idname = "seqvel.hover_cursor"
    # 静态层常驻英中双语（ADR-0009 修订 R1）
    bl_label = "Speed line hover cursor 速度线悬停光标"
    bl_description = ("(internal) Switch to the HAND_POINT cursor while the "
                      "mouse hovers over the speed line; consumes no events "
                      "（内部）鼠标悬在速度线上时切换 HAND_POINT 光标；不消费任何事件")
    # 不挂 REGISTER/UNDO：不改数据、不进撤销栈、不出现在搜索里
    bl_options = set()

    def invoke(self, context, event):
        try:
            return self._start(context, event)
        except Exception as exc:
            # 悬停出错也绝不吃事件；面板说真话（"不静默"纪律）
            status.set_error(i18n.T("hover.update_failed", exc))
            return {"PASS_THROUGH"}

    def _start(self, context, event):
        if event is None or getattr(event, "type", "") != "MOUSEMOVE":
            return {"PASS_THROUGH"}  # F3/脚本直调没有真实鼠标移动，不算
        win = getattr(context, "window", None)
        mouse_x = getattr(event, "mouse_x", None)
        mouse_y = getattr(event, "mouse_y", None)
        if win is None or mouse_x is None or mouse_y is None:
            return {"PASS_THROUGH"}
        if status.STATE["editing"]:
            return {"PASS_THROUGH"}  # 拖动中不碰光标（HAND_POINT 保持到松手）

        # [诊断 v1.2.4→R6] 空闲期的选择/激活哨兵：快照变了就留痕，并把
        # 结构化快照常驻缓存（按下前状态，供拖动恢复用）。
        global last_sel_data
        data = status.sel_data(context)
        if data is not None:
            if data != last_sel_data:
                status.trace("selchange -> [%s] sel=[%s] active=%s"
                             % (data["scene"], ",".join(sorted(data["selected"])),
                                data["active"]))
                last_sel_data = data
            elif last_sel_data is None:
                last_sel_data = data

        key = _key(win)
        was_on = key in _set_windows
        want = _decide(context, mouse_x, mouse_y)
        if want and not was_on:
            _set_cursor(win, CURSOR_ON)
            _set_windows[key] = win
            status.trace("hover -> ON (%.0f,%.0f)" % (mouse_x, mouse_y))
        elif not want and was_on:
            _set_cursor(win, CURSOR_OFF)
            _set_windows.pop(key, None)
            status.trace("hover -> off (%.0f,%.0f)" % (mouse_x, mouse_y))
        return {"PASS_THROUGH"}
