# -*- coding: utf-8 -*-
"""条带坐标系与命中检测（纯 Python，零 bpy）。

时间轴上的像素坐标一律通过 ``region.view2d`` 换算（``view_to_region`` /
``region_to_view``），本模块只依赖这两个方法，因此可以用假对象在单测里验证：

- view 坐标：``x = 时间轴帧``，``y = 通道``（浮点，整数通道落在整数带内）；
- 速度线垂直映射：``0 ~ 显示范围上限`` 铺满**标尺区间**（通道高的 9%~95%：
  Blender 绘制区 5%~95% 的下沿再留 4% 底部留白，ADR-0010 修订 R1；映射本体
  在 ``curve`` 的 ``speed_to_yfrac`` / ``yfrac_to_speed``，本模块不做任何
  二次变换），即默认 3.0 档下 1.0x 落在标尺 1/3 处（标尺档位见
  CONTEXT.md「显示范围上限」，运行时由 ``curve.ACTIVE_CAP`` 承载）。
"""

from collections import namedtuple

from .curve import (active_cap, speed_to_yfrac,
                    yfrac_to_speed, yfrac_to_speed_raw)

#: 命中类型
HIT_NONE = "NONE"
HIT_POINT = "POINT"
HIT_SEGMENT = "SEGMENT"
HIT_TAIL = "TAIL"

#: 命中半径（像素）
POINT_RADIUS = 12.0
LINE_RADIUS = 8.0

#: ``distance`` = 鼠标到命中特征的像素距离（跨条带择近的依据，ADR-0005）；
#: ``HIT_NONE`` 时为 ``None``。三参构造与旧二参构造都合法。
Hit = namedtuple("Hit", "kind index distance")
Hit.__new__.__defaults__ = (None,)


def view_to_region(view2d, frame, channel):
    """view 坐标（帧, 通道）→ 区域像素；无法换算返回 None。

    用不裁剪模式（**关键字** ``clip=False``）：点落在视口外也照常换算出
    坐标，可见性由绘制方用 GPU scissor 统一裁剪（``view2d_mask`` +
    ``overlay`` 的绘制回调）——GPU 并不会自动裁掉出界图元。

    ``clip`` 是 RNA 关键字参数（5.2.1 实测）：位置传参直接 TypeError。
    旧代码在这里栽过跟头——位置传参失败后被静默兜底成 2 参调用，等效
    clip=True，而出视口的点在 5.2.1 返回的是哨兵值 (12000, 12000) 而非
    None，出界曲线段于是全被画向那个远处的固定点，正是"速度线往远处
    伸"这个 bug 的根源。现在主路径走关键字 clip=False（真机必然命中）；
    2 参兜底只服务于"根本没有 clip 参数"的 view2d 替身/宿主，真机上不可达。
    也不能用 clip=True 兜底：且不说哨兵值，真返回 None 的实现会让
    ``curve_xy`` 因任一点失败而整体放弃，等于"条带一侧出屏就整条速度线
    消失"。
    """
    if view2d is None:
        return None
    try:
        result = view2d.view_to_region(frame, channel, clip=False)
    except TypeError:
        try:
            result = view2d.view_to_region(frame, channel)
        except Exception:
            return None
    except Exception:
        return None
    if result is None:
        return None
    try:
        x, y = float(result[0]), float(result[1])
    except (TypeError, ValueError, IndexError):
        return None
    if x != x or y != y:
        return None
    return x, y


def region_to_view(view2d, x, y):
    """区域像素 → view 坐标（帧, 通道）；失败返回 None。"""
    if view2d is None:
        return None
    try:
        result = view2d.region_to_view(x, y)
        return float(result[0]), float(result[1])
    except Exception:
        return None


# --------------------------------------------------------------------------
# 视口裁剪（GPU scissor 的裁剪矩形）
# --------------------------------------------------------------------------

#: 每条边二分的步数：步数 n 后误差约为（边到中心的像素距离）/ 2^n，
#: 40 步在 2000px 宽的区域里已到 1e-9 像素量级，远低于光栅化精度
_PROBE_STEPS = 40


def _finite_pair(pair):
    """二元坐标是有限数值（非 None、非 NaN）。"""
    return (pair is not None
            and pair[0] == pair[0] and pair[1] == pair[1])


def _view_to_region_clipped(view2d, frame, channel):
    """裁剪模式（clip=True）探测：点出视口返回 None；不支持探测也返回 None。

    5.2.1 实测：clip=True 出视口返回哨兵值 (12000, 12000)，**不是** None
    （更老的实现才返回 None）。因此不能只看返回值——把 clip=True 与不裁剪
    换算（``view_to_region``）的结果对比，两者明显不一致即判为出视口。
    这样同时兼容"返回 None""返回哨兵""钳制到边界"三种上游实现，
    不依赖哨兵的具体取值。
    """
    exact = view_to_region(view2d, frame, channel)
    if exact is None:
        return None
    try:
        result = view2d.view_to_region(frame, channel, clip=True)
    except TypeError:
        return None  # 只有旧版双参签名：无法探测，交给调用方兜底
    except Exception:
        return None
    if result is None:
        return None
    try:
        x, y = float(result[0]), float(result[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not _finite_pair((x, y)):
        return None
    # 出视口：clip=True 与真实换算显著不同（哨兵 / 钳制）→ 判为不可见
    if abs(x - exact[0]) > 0.5 or abs(y - exact[1]) > 0.5:
        return None
    return x, y


def _bisect_view_edge(view2d, inside, outside):
    """在 view 坐标里二分找"裁剪换算由有值翻转为 None"的边界点。

    ``inside`` 必须在视口内（clip 换算有值），``outside`` 必须出视口
    （clip 换算返回 None）；两点须同水平或同垂直（只沿一个轴推进）。
    返回边界处的 view 坐标；探测失败返回 None。
    """
    good = [inside[0], inside[1]]
    bad = [outside[0], outside[1]]
    for _ in range(_PROBE_STEPS):
        mid = [(g + b) / 2.0 for g, b in zip(good, bad)]
        if _view_to_region_clipped(view2d, mid[0], mid[1]) is None:
            bad = mid
        else:
            good = mid
    return ((good[0] + bad[0]) / 2.0, (good[1] + bad[1]) / 2.0)


def view2d_mask(view2d, region_width, region_height):
    """时间轴 view2d 的可见区（网格 widget 矩形，不含滚动条），区域像素。

    Blender 的 Python API 不暴露 View2D 的 mask，这里用「裁剪探测 + 二分」
    把它量出来：探测函数把"点出视口"归一成 None（兼容上游返回 None、
    哨兵 (12000,12000)、钳制三种实现，见 ``_view_to_region_clipped``），
    据此对四条边各做一次二分，得到视口边界的 view 坐标，再用不裁剪换算
    换回像素。mask 是区域固定的，缩放 / 横移只改 cur，因此结果与视图
    状态无关——正好就是"应该被遮住"的区域。

    探测任一环节不支持或失败返回 None（调用方退回整个区域矩形兜底：
    裁不到滚动条那条缝，但"往远处伸"仍然被止住）。
    """
    if view2d is None:
        return None
    try:
        width, height = float(region_width), float(region_height)
    except (TypeError, ValueError):
        return None
    if not (width > 0.0 and height > 0.0):
        return None

    center = region_to_view(view2d, width / 2.0, height / 2.0)
    if not _finite_pair(center):
        return None
    cx, cy = center
    # 中心点必须真的在视口内（退化 mask / 空视图直接放弃）
    if _view_to_region_clipped(view2d, cx, cy) is None:
        return None

    # 各方向取一个"必定出视口"的远点：mask 不会超出区域，区域外一倍
    # 尺寸足够。与中心同水平/垂直，保证二分只沿一个轴推进。
    far_points = (
        region_to_view(view2d, -width, height / 2.0),
        region_to_view(view2d, 2.0 * width, height / 2.0),
        region_to_view(view2d, width / 2.0, -height),
        region_to_view(view2d, width / 2.0, 2.0 * height),
    )
    for point in far_points:
        if not _finite_pair(point):
            return None

    try:
        left = _bisect_view_edge(view2d, center, far_points[0])[0]
        right = _bisect_view_edge(view2d, center, far_points[1])[0]
        bottom = _bisect_view_edge(view2d, center, far_points[2])[1]
        top = _bisect_view_edge(view2d, center, far_points[3])[1]
    except Exception:
        return None

    # 边界的 view 坐标换回像素（不裁剪模式，轴对齐所以另一维无所谓）
    xmin_y = view_to_region(view2d, left, cy)
    xmax_y = view_to_region(view2d, right, cy)
    y_xmin = view_to_region(view2d, cx, bottom)
    y_xmax = view_to_region(view2d, cx, top)
    if None in (xmin_y, xmax_y, y_xmin, y_xmax):
        return None
    xmin, xmax = xmin_y[0], xmax_y[0]
    ymin, ymax = y_xmin[1], y_xmax[1]

    # 兜底钳制：mask 理应 ⊆ 区域；探测行为异常时最多退化成区域矩形
    xmin = max(xmin, 0.0)
    ymin = max(ymin, 0.0)
    xmax = min(xmax, width)
    ymax = min(ymax, height)
    if not (xmax > xmin and ymax > ymin):
        return None
    return (xmin, ymin, xmax, ymax)


def point_xy(view2d, base_frame, channel, rel_frame, speed):
    """条带上 (相对帧, 速度) → 区域像素。"""
    return view_to_region(view2d, base_frame + rel_frame, channel + speed_to_yfrac(speed))


def curve_xy(view2d, base_frame, channel, points):
    """整条曲线的像素折线；任一点无法换算返回 None。"""
    out = []
    for rel_frame, speed in points:
        xy = point_xy(view2d, base_frame, channel, rel_frame, speed)
        if xy is None:
            return None
        out.append(xy)
    return out


def baseline_xy(view2d, base_frame, channel, duration):
    """1.0x 基线的两个端点。"""
    start = point_xy(view2d, base_frame, channel, 0.0, 1.0)
    end = point_xy(view2d, base_frame, channel, duration, 1.0)
    if start is None or end is None:
        return None
    return [start, end]


def tail_end_xy(view2d, base_frame, channel, duration, last_speed):
    """尾段水平延长线的终点像素。"""
    return point_xy(view2d, base_frame, channel, duration, last_speed)


#: 画线与命中共用的采样密度：每约多少像素一小段
SAMPLE_PIXELS = 2.0
#: 采样步长下限（帧）：极度放大时也别把段切得过碎
MIN_SAMPLE_STEP = 0.25


def sample_step(view2d):
    """绘制 / 命中用的采样步长（帧）：按 view2d 横向比例折算成约 ``SAMPLE_PIXELS`` 像素。

    取不到比例（假对象 / 异常）时退回 1 帧。画线与命中都用这个步长，
    才能保证"看得见的弯"就是"抓得到的弯"。
    """
    origin = region_to_view(view2d, 0.0, 0.0)
    offset = region_to_view(view2d, SAMPLE_PIXELS, 0.0)
    if origin is None or offset is None:
        return 1.0
    step = abs(offset[0] - origin[0])
    if step != step or step <= 0.0:
        return 1.0
    return max(MIN_SAMPLE_STEP, step)


def mouse_to_view(view2d, mouse):
    """鼠标位置 → (相对帧, 速度)，与拖点共用同一套映射。"""
    result = region_to_view(view2d, mouse[0], mouse[1])
    if result is None:
        return None
    return result


def mouse_to_rel_speed(view2d, base_frame, channel, mouse):
    """鼠标位置 → (相对条带起点的帧, 该处速度)。"""
    result = mouse_to_view(view2d, mouse)
    if result is None:
        return None
    frame, chan = result
    return frame - base_frame, yfrac_to_speed(chan - channel)


def mouse_to_rel_speed_raw(view2d, base_frame, channel, mouse):
    """鼠标位置 → (相对条带起点的帧, **未钳**速度)——只喂锚定拖动会话。

    [R11] 与 :func:`mouse_to_rel_speed` 同一条标尺、同一换算率，但速度
    不做 ``[SPEED_MIN, cap]`` 钳制：光标越过条带上/下沿后，映射值按同一
    换算率继续外插（超 cap / 为负），锚定会话的增量才能越界继续增长
    （ADR-0005 修订 R11「锚定输入去饱和」）。绝对吸附、插点等"点到光标
    处"的路径**不要**吃它——那些路径要的正是钳制后的标尺值。
    """
    result = mouse_to_view(view2d, mouse)
    if result is None:
        return None
    frame, chan = result
    return frame - base_frame, yfrac_to_speed_raw(chan - channel)


def mouse_to_rel(view2d, base_frame, mouse):
    """鼠标位置 → 相对条带起点的帧。"""
    result = mouse_to_view(view2d, mouse)
    if result is None:
        return None
    return result[0] - base_frame


def dist(ax, ay, bx, by):
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def dist_point_segment(px, py, ax, ay, bx, by):
    """点到线段的距离。"""
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length_sq = vx * vx + vy * vy
    if length_sq <= 0.0:
        return dist(px, py, ax, ay)
    ratio = max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    return dist(px, py, ax + ratio * vx, ay + ratio * vy)


def hit_test(view2d, base_frame, channel, duration, curve, mouse,
             point_radius=POINT_RADIUS, line_radius=LINE_RADIUS):
    """命中检测，优先级：速度点 > 尾段延长线 > 普通段。

    类别之间按上面的优先级；**同一类里有多个候选时一律取最近**（像素距离最小者，
    平手取下标较小者）—— 与 ``pick_hit`` 的跨条带择近是同一条纪律。速度线在尖拐点
    附近折得很近时，点与段都可能"两个都在半径内"，此时"最近"才是用户指的那一个。

    **线段命中吃的是 ``curve.sampled_points()`` 的采样折线**（与 overlay 画线同源），
    所以缓动段上"看得见的弯"必定抓得到；返回的段下标经 ``owners`` 映射回**模型段号**，
    拖动仍按模型段来。
    """
    points = curve.points
    if not points or mouse is None:
        # 没有鼠标位置就谈不上命中（事件缺坐标、或上下文里没有时间轴区域）
        return Hit(HIT_NONE, None)
    samples, owners = curve.sampled_points(sample_step(view2d))
    pixel_samples = curve_xy(view2d, base_frame, channel, samples)
    pixel_points = curve_xy(view2d, base_frame, channel, points)
    if pixel_samples is None or pixel_points is None:
        return Hit(HIT_NONE, None)
    mouse_x, mouse_y = float(mouse[0]), float(mouse[1])

    best_index, best_distance = None, point_radius
    for index, (px, py) in enumerate(pixel_points):
        distance = dist(mouse_x, mouse_y, px, py)
        if distance < best_distance:
            best_index, best_distance = index, distance
    if best_index is not None:
        return Hit(HIT_POINT, best_index, best_distance)

    tail_end = curve.tail_extension_end(duration)
    if tail_end is not None:
        end_xy = tail_end_xy(view2d, base_frame, channel, tail_end, points[-1][1])
        if end_xy is not None:
            last_x, last_y = pixel_points[-1]
            tail_distance = dist_point_segment(mouse_x, mouse_y, last_x, last_y,
                                               end_xy[0], end_xy[1])
            if tail_distance < line_radius:
                return Hit(HIT_TAIL, None, tail_distance)

    # 段命中同样**取最近**——与上面的点循环、与 ``pick_hit`` 的跨条带择近
    # 是同一条纪律。原先写的是"第一个进半径就 return"，从下标 0 起遍历，
    # 于是永远偏向下标小的那一段、与鼠标距离无关：尖拐点（段内角小于约 84°）
    # 附近两段会同时进 line_radius，症状是右键改错段的形状、**拖错段**（会改数据）。
    # 平手（逐位等距）时靠"严格 < + 升序遍历"保住下标较小者，与遍历顺序一致、可预测。
    best_piece, best_piece_distance = None, line_radius
    for piece in range(len(pixel_samples) - 1):
        ax, ay = pixel_samples[piece]
        bx, by = pixel_samples[piece + 1]
        segment_distance = dist_point_segment(mouse_x, mouse_y, ax, ay, bx, by)
        if segment_distance < best_piece_distance:
            best_piece, best_piece_distance = piece, segment_distance
    if best_piece is not None:
        owner = owners[best_piece] if best_piece < len(owners) else 0
        return Hit(HIT_SEGMENT, owner, best_piece_distance)

    return Hit(HIT_NONE, None, None)


def pick_hit(candidates):
    """从多个条带的命中结果里挑一个（ADR-0005：同屏重叠取最"顺手"的）。

    ``candidates`` 是 ``(strip, hit)`` 对；返回 ``(strip, hit)``，全没命中返回
    ``(None, None)``。优先级：**速度点 > 线段/尾段**（点是明确意图，要压过
    恰好横穿它的别的线），同类再按像素距离取最近。
    """
    best = None
    best_key = None
    for strip, hit in candidates:
        if hit is None or hit.kind == HIT_NONE or hit.distance is None:
            continue
        key = (0 if hit.kind == HIT_POINT else 1, hit.distance)
        if best_key is None or key < best_key:
            best, best_key = (strip, hit), key
    return best if best is not None else (None, None)


def speed_range_label():
    """HUD / 面板用的显示范围文案（跟随当前显示范围上限）。"""
    return "0 ~ %.1fx" % active_cap()
