# VSE 场景读取一律走 displayed_scene，不直接读 context.scene

## Status

accepted —— 推翻 v1.0.7 的修法（当时臆造了 `SpaceSequenceEditor.scene` 属性，
实际该属性在 5.2.1 RNA 里不存在，整段是死代码；教训：GUI 宿主 API 假设必须
探针验证）。

## 背景

用户真机故障（5.blend，Blender 5.2.1）：面板永远悬停在一条 19468 帧的 mp3 上
（"音频条带不能编辑"），速度线画不出、点条带没反应——而用户明明选中了
Scene.001。

根因：**Blender 5.0 起，VSE 时间线显示的不再是窗口的活动场景，而是工作区上
单独指定的「序列场景」（Sequencer Scene）**。头部场景选择器写的是
`workspace.sequencer_scene`，且 `context.sequencer_scene` 直接暴露它。用户的
文件里活动场景「Scene」（只有 mp3，fps60）与序列场景「场景」（干活条带全在
这，fps24）恰好各是各的——插件全链路读 `context.scene`，于是永远盯住 mp3。

## 决定

新增 `regions.displayed_scene(context)`，三级回退：

1. `context.sequencer_scene`（5.x 直接暴露，无 space_data 时也可信）；
2. `workspace.sequencer_scene`（仅当 `space_data.type == 'SEQUENCE_EDITOR'`，
   非 VSE 空间不采信）;
3. `context.scene`（4.x 行为 / 裸环境兜底）。

**全插件的场景读取收敛到这一个函数**——扫描（`scan_targets`）、绘制
（overlay / HUD）、面板（目标解析、诊断、设置读写）、操作符（缓存开关等）
一律经它，一处修复全链路生效。面板在显示场景 ≠ 活动场景时显示提示行
「编辑场景「X」（活动场景「Y」）」，避免这种错位再被当成插件认错条带。

## 考虑过的选项

- **读 `SpaceSequenceEditor.scene`**（v1.0.7 的做法）：否。该属性不存在，
  是臆造的 API——任何"宿主应该有某属性"的假设都必须先探针验证再进代码。
- **只在 `scan_targets` 一处修**：否。场景读取散布在 carrier / overlay / ui /
  多个操作符里，只修一处则其余路径（HUD、诊断、缓存开关）继续看错场景，
  故障会换个面目复发。

## 后人不该"简化"回去的理由

`context.scene` 在单场景文件里**恰好总是对的**，于是顺手写回去测试全绿、
真机也看不出来——直到用户像 5.blend 一样把序列场景和活动场景分开用。
判定标准不是"多数文件只有一个场景"，而是"VSE 显示的是哪个场景"。
回归测试：`tests/test_pinned_scene.py`（7 用例，假 context 覆盖全部回退分支）。
