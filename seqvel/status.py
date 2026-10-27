# -*- coding: utf-8 -*-
"""插件运行状态（面板文案 / 诊断的单一出口）。

绘制回调与 modal 交互都会往这里写，面板只读这里，避免诊断信息散落各层。
常驻提示与模式名走 :mod:`i18n`（运行层，随语言模式切换；ADR-0009）。
"""

from . import i18n


def hint_idle():
    """空闲态提示（必须说明直接操纵的用法；只显示给还没建线的条带）。"""
    return i18n.T("status.hint_idle")


def hint_edit():
    """拖动中的头部提示。"""
    return i18n.T("status.hint_edit")


def mode_label_for(mode):
    """模式名（面板显示用）。"""
    return i18n.T("status.mode_%s" % (mode or "idle"))

STATE = {
    "hint": "",
    "mode": "",
    "editing": "",
    "error": "",
    "diag": None,
    "draw_calls": 0,
    "draw_reason": "",
    #: 最近一次命中检测的结果（NONE / POINT / SEGMENT / TAIL）。
    #: 面板把它摆出来：编辑中怎么点都没反应时，一眼能分清是"根本没命中"
    #: 还是"命中了但没动"——前者几乎总是坐标来源不对。
    "last_hit": "",
    "keymap": "",
    #: [修订 R10] 本次拖动正在改变的点：``(strip_key, (下标, ...))`` 或 None。
    #: 交互在按下时写、收摊时清；绘制层读它决定"临时读数"亮哪几个点。
    "drag": None,
}


def set_hint(text):
    STATE["hint"] = text or ""


def set_mode(mode):
    STATE["mode"] = mode or ""


def set_editing(strip_name):
    STATE["editing"] = strip_name or ""


def set_error(text):
    STATE["error"] = text or ""


def clear_error():
    STATE["error"] = ""


def note_write(diag):
    STATE["diag"] = diag


def note_draw(reason):
    STATE["draw_calls"] += 1
    STATE["draw_reason"] = reason or ""


def note_hit(kind):
    STATE["last_hit"] = kind or ""


def note_keymap(text):
    """键位挂载健康位（如 "3/3"），面板常显；空串 = 注册流程没跑过。"""
    STATE["keymap"] = text or ""


def set_drag_target(key, indices):
    """[修订 R10] 公布"本次拖动正在改变哪些点"，供绘制层决定临时读数。

    ``key`` 用 ``store.strip_key`` 那套（场景名, 条带名）——但**由调用方算好传
    进来**：本模块保持零 import（store import status，反向会成环）。
    空集合 = 没有点要显形，与"没在拖动"统一收敛成 None（绘制端只需判 None）。
    """
    indices = tuple(indices) if indices else ()
    if key is None or not indices:
        clear_drag_target()
        return
    STATE["drag"] = (key, indices)
    trace("drag-target key=%r indices=%r" % (key, indices))


def clear_drag_target():
    """收摊：清掉拖动目标（临时读数随之消失）。"""
    if STATE["drag"] is not None:
        trace("drag-target cleared")
    STATE["drag"] = None


def drag_target():
    """当前拖动目标 ``(strip_key, (下标, ...))``；空闲为 None。"""
    return STATE["drag"]


def sel_data(context):
    """[修订 R6] 结构化选择/激活快照：{"scene", "selected", "active"}。

    读不到给 None 字段，绝不抛。sel_snapshot 的数据源。
    """
    try:
        scene = getattr(context, "scene", None)
        se = getattr(scene, "sequence_editor", None)
        if se is None:
            return None
        selected = set()
        try:
            for s in se.strips_all:
                if getattr(s, "select", False):
                    selected.add(s.name)
        except Exception:
            pass
        active = getattr(se, "active_strip", None)
        return {"scene": getattr(scene, "name", "?"),
                "selected": selected,
                "active": getattr(active, "name", None)}
    except Exception:
        return None


def sel_snapshot(context):
    """[诊断 v1.2.4] 选择/激活快照：谁被选中、谁是 active_strip。

    只服务排障（seqvel_trace.log）；必须绝对不抛（快照挂了不能连累交互）。
    """
    data = sel_data(context)
    if data is None:
        return "no-se"
    return "[%s] sel=[%s] active=%s" % (data["scene"],
                                        ",".join(sorted(data["selected"])),
                                        data["active"])


def trace(msg):
    """[诊断 v1.2.3] 把交互轨迹追加写进临时文件，供真机排障读取。

    只在"按下/挂载"这类低频节点上调用；写失败静默（诊断不能反过来
    干扰交互）。用 TEMP 下的 seqvel_trace.log，跨会话可读。
    """
    import os
    import time
    try:
        path = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                            "seqvel_trace.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%.3f %s\n" % (time.time(), msg))
    except Exception:
        pass


def mode_label():
    return mode_label_for(STATE["mode"])


def reset():
    STATE["hint"] = ""
    STATE["mode"] = ""
    STATE["editing"] = ""
    STATE["diag"] = None
    STATE["last_hit"] = ""
    STATE["keymap"] = ""
    STATE["drag"] = None
