# -*- coding: utf-8 -*-
"""可以/不可以变速的条带类型规则（纯 Python，零 bpy）。

只吃条带类型字符串，方便在无 Blender 环境下单测；bpy 侧把 ``strip.type``
传进来即可。
"""

#: 可以作为目标的源条带类型（自身产出画面）
SOURCE_TYPES = ("MOVIE", "IMAGE", "SCENE", "MOVIECLIP", "MASK", "META")
#: 音频条带（Speed Control 不处理音频）
AUDIO_TYPE = "SOUND"
#: 效果条带（含本插件自己的承载条带）——不能作为目标
EFFECT_TYPES = (
    "CROSS", "ADD", "SUBTRACT", "ALPHA_OVER", "ALPHA_UNDER", "GAMMA_CROSS",
    "COMPOSITOR", "MULTIPLY", "WIPE", "GLOW", "COLOR", "SPEED", "MULTICAM",
    "ADJUSTMENT", "GAUSSIAN_BLUR", "TEXT", "TRANSFORM", "COLORMIX",
)

from . import i18n


def can_edit_type(strip_type):
    """返回 ``(是否可编辑, 说明文案)``（文案走运行层 i18n）。"""
    kind = str(strip_type or "")
    if kind == AUDIO_TYPE:
        return False, i18n.T("rules.audio_sound")
    if kind in SOURCE_TYPES:
        return True, ""
    if kind in EFFECT_TYPES:
        return False, i18n.T("rules.refuse_effect")
    return False, i18n.T("rules.refuse_type", kind or i18n.T("rules.unknown_type"))


def audio_note_type(strip_type):
    """音频相关提示；无提示返回 None。"""
    kind = str(strip_type or "")
    if kind == AUDIO_TYPE:
        return i18n.T("rules.audio_sound")
    if kind == "MOVIE":
        return i18n.T("rules.audio_movie")
    return None


def visible_strips(strips, active_strip, has_model):
    """速度线的可见（= 可抓）条带集合（ADR-0005：所见即可抓）。

    判定：源类型 **且** 满足以下任一——

    - 建过速度曲线（``has_model(strip)``）：常开显示、随时可抓；
    - 被选中或为活动条带：显示默认线，此时可抓。

    没建过线又未选中的条带**不在**集合里：它的默认线根本没画，若参与命中
    就成了"看不见却能抓住的线"，空白点击会被莫名吃掉。绘制（``overlay``）
    与命中检测（``interaction``）共用本函数，两个集合因此永不脱节。

    ``has_model`` 是 ``strip -> bool`` 调用（bpy 侧传 ``STORE.has_model``）；
    纯 Python、零 bpy，入参只要求鸭子类型（``type`` / ``select``）。
    """
    result = []
    for strip in strips:
        if str(getattr(strip, "type", "") or "") not in SOURCE_TYPES:
            continue
        if (has_model(strip) or getattr(strip, "select", False)
                or (active_strip is not None and strip == active_strip)):
            result.append(strip)
    return result


#: 取帧策略（**内部标识**，恒定英文：比较/判据只认它，不许拿显示文案当键）。
#: 面板显示经 ``i18n``（``rules.sampling_*``）随语言模式切换。
SAMPLING_SUBFRAME = "subframe"
SAMPLING_SNAP = "snap"
#: 唯一把小数源位置保留到渲染器的源条带类型（``render.cc`` 的
#: ``RE_RenderFrame(..., floorf(frame), subframe)``）；其余类型渲染前
#: ``round_fl_to_int``。META 是否继承其内嵌套场景条带的连续性未取证。
SUBFRAME_TYPES = ("SCENE",)


def sampling_policy(strip_type):
    """该类型条带的取帧策略：决定非整数倍速下是连续还是重复帧。

    与 Speed Control 的混合帧开关无关——插件恒定关闭混合帧，这里描述的是
    "小数源位置最终被谁消费"。面板据此把"为什么有的素材顺、有的素材顿"
    讲清楚（显示文案在 ``i18n``，随语言切换）。
    """
    kind = str(strip_type or "")
    return SAMPLING_SUBFRAME if kind in SUBFRAME_TYPES else SAMPLING_SNAP


def sampling_note(strip_type):
    """取帧策略要提醒用户的代价（运行层文案）；没有代价返回 None。"""
    return i18n.T("rules.note_snap") if sampling_policy(strip_type) == SAMPLING_SNAP else None


def sampling_label(policy):
    """取帧策略的显示名（面板用）。"""
    key = ("rules.sampling_subframe" if policy == SAMPLING_SUBFRAME
           else "rules.sampling_snap")
    return i18n.T(key)


def subframe_limited(sampling, cache_raw):
    """子帧渲染是否正被 Raw 缓存吞掉。

    ``sampling`` 是 :func:`sampling_policy` 的结果，``cache_raw`` 是本场景
    ``SequenceEditor.use_cache_raw`` 的读取值（``None`` = 读不到）。

    只有**本来就是子帧渲染**的素材（场景条带）才会被缓存吃掉源时间；取整选帧的
    素材本来就不消费小数，缓存开与不开一个样。``cache_raw`` 为 ``None`` 时返回
    ``False``：读不到开关不能凭空断定"受限"（面板会把异常原文单独写出来）。
    """
    return sampling == SAMPLING_SUBFRAME and cache_raw is True
