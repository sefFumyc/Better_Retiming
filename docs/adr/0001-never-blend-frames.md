# 永不混合帧：速度线的帧精度策略定为取整选帧 / 子帧渲染

## Status

accepted —— 推翻 v1 的 `use_frame_interpolate=True`（当时的理由写作"平滑优先，接受轻微混合帧"）。

## 背景

速度线落地走 Speed Control 效果条带（`speed_control='MULTIPLY'` + `speed_factor` 关键帧），
Blender 侧把关键帧**逐帧积分**成源帧映射（`frameMap`），所以每个输出帧的源位置几乎必然是小数。
这个小数怎么被消费，只由一个标志决定：

- **开（`use_frame_interpolate=True`）**：`do_speed_effect` 取 `floor` / `ceil` 两个整数源帧，
  走一次 `cross` 溶解，权重 **α = 源位置的小数部分**。观感即重影（用户报的"帧混合"）。
  附带损失：`render.cc` 会把输入帧索引先 `floor` 掉，等于顺手放弃场景素材的子帧精度。
- **关**：小数源位置原样传给输入条带 —— 场景条带走 `RE_RenderFrame(..., subframe)` 子帧渲染
  （源时间连续，真·无级变速）；movie / image / movieclip 渲染前 `round_fl_to_int` 取整选帧。

原生 Retiming 与本策略无关：它整条链路只做数学映射，**没有任何混合代码**，所以它天生只有
"取整/连续"，没有"重影"这个选项。

## 决定

`USE_FRAME_INTERPOLATE = False`，并且**不提供面向用户的开关**。承载条带每次落地都无条件
重写这个标志（老 `.blend` 里开着的会被顺手关掉；策略不是曲线数据，不进影子模型）。

## 考虑过的选项

- **保留开关、默认关**（尤其"只在 movie/image 素材上暴露"）：被否。场景素材上关掉是零代价，
  开关唯一的实际作用是给 movie/image 素材在"顿挫"与"重影"之间选一个；而重影是我们明确不要的
  产物，不该给它留入口。
- **保持混合以换取平滑**：被否。对场景素材它是纯损失；对 movie/image 素材，真正的"平滑且不重影"
  要靠运动矢量重采样（光流），Speed Control 不做，自研不在 v1 范围。

## 后人不该改回去的理由

`render.cc::seq_render_effect_strip_impl` 里那句注释
`Only convert to int when interpolation is not used.` 与它下面紧挨着的判断条件**写反了**
（实际是"打开插值时才 `floor`"）。"打开混合会更平滑"的直觉在这条链路上不成立 —— 读代码时别被
那句注释带走。
