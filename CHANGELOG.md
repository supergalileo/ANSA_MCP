# 变更记录

按时间倒序。每条只写「改了什么、为什么」，不写「优化了体验」这类空话。

## 2026-11-22

### 新增：模型状态三工具（`ansa_snapshot` / `ansa_diff` / `ansa_verify`）

起因：焊缝四边形模式把 **1 971 个板单元**从原属性改挂到新属性，单元总数一个没变，
纯靠数实体发现不了，是临时手写快照才看见的。这类改动不该靠运气发现。

- `ansa_snapshot(name)`：记录实体数、单元→属性映射、属性表（厚度/材料），
  重型映射存放在 ANSA 内的 `ansavars`，只回传计数。
- `ansa_diff(name)`：逐单元比对，报增/删/改属性，附样本 id 与属性增删。
- `ansa_verify(checks)`：`counts` / `props` / `mats` / `rbe` / `free` / `quality` 六项自研，
  外加 `native` 组直接调 ANSA Checks Manager；实测 63 000 壳上 `free` 0.6 s、`quality` 1.5 s、`native` 2.3 s。

实测：故意改 3 个壳的属性 → `diff` 精确报 3，增删均 0；
`quality` 首版用 XY 投影算面积，把竖直壳全误判成零面积（2 394 个），改 Newell 矢量后归零。

新增 `tests/test_checks.py`（离线 25 项），锁住三个契约：脚本可解析、
**最后一句必须是表达式**（否则 runner 取不到返回值）、质量公式必须是三维面积。

### 新增：`ansa_verify` 接入 ANSA 自带 Checks Manager（`native` 组）

ANSA 把约 130 个检查暴露在 `ansa.base.checks.<general|geometry|mesh|penetration>.<Name>()`，
每个返回一个 `Check`，`execute()` 给出 `[CheckReport]`，报告里带 `status` / `message` /
`err_code` / `entities`。全部可在无界面下调用。

在参考模型（63 338 壳）上一次跑 10 项，2.4 秒：

| 检查 | 结果 |
|---|---|
| 自由节点 / 未定义材料 / 未定义属性 / RBE3 定义 / 非流形节点 | ok |
| 连接检查 | error，154 条：E3881 厚度 84、E3877 间距 43、E3880 夹角 25、E3875 投影失败 2 |
| 重复单元 | warning，1 |
| 网格质量 | error：warping 283、skewness 15、min angle 13、jacobian 8 超标 |
| 干涉 | error，4 |
| 邻近度 Proximities | error，4：两条零件间穿插（E3966/E3967），直接点出 PID 对 |

**定位：参考项，不是门槛。** ANSA 判据偏严——283 个翘曲单元、154 条连接备注，
模型一直正常在用。这一组的价值是“指出该看哪里、拿什么去谈”，不是判模型能不能用。
返回结构里带 `advisory: true` 与说明，避免使用者误当通过条件。
真正在任何口径下都必须成立的是 `free` / `mats` / `rbe` 三项。

另一层意义：此前手写的六项检查对这 154 条全部 pass。自研检查覆盖“结构是否自洽”，
ANSA 的检查覆盖“按它的规范是否有问题”——两者是互补，不是替代。

### 新增：`skills/ansa-seam-weld`（焊缝连接技能）

四种表示：RBE2 刚性、CBAR 弹性、实体 CHEXA+RBE3、`OVERLAP-SHELL` 共节点四边形。

- 连接管理器路线：焊道提中心线 → `CreateCurve` → `SeamLine_Type` → 阶梯挂件 → `SetWeldPosition` → `RealizeConnections`
- 实体模式：`W`（宽度）默认 0 会静默建不出，`Height` 才是厚度（1/2/3 mm 实测精确对应）
- 四边形模式：只有 `OVERLAP-SHELL` 出共节点跨板单元；`RootShells` 默认双排，本技能改为单排
- 容差无效的实证：`SearchDist` 5/10/20 与 `Parameters_Gap` 0.5–5 对 22 条顽固焊缝全部 0 条，默认反而 1 条
- 二级回退：`RBE3-SHELL-RBE3` 覆盖剩余 22 条，全模型 133/133 无遗漏
- 属性还原：`restore_properties=True` 把被改挂的板单元写回原 PID，实测 1 236 改 / 1 236 还原

### 重写：`USAGE.md`

原标题只讲一个技能，正文 0 次提及焊缝。改为覆盖 22 个工具 + 三个技能 + 验收门槛 + 排错表，
新增「先学会验模型」一节；「边界」里删掉过时的「连接建模得另外定义」，改为点名点焊/胶接/包边尚未实现。

## 2026-11-21

### 新增：`skills/ansa-seam-weld` 初版

- `seam_extract` / `seam_build` / `seam_chunks` / `seam_final` / `seam_audit`
- 关键实测：`RealizeConnections` 不校验 FE 名——未知名字静默回落 RBE2，
  而已知但建不出的模式返回 0，两者症状不同，据此可以反推模式是否有效
- 143 根焊道、145 条中心线，最终 133 条连接、2 252 个 RBE2、0 自由节点

## 更早

上一版只包含 `ansa-midsurface-mesh`（抽中面 + 划网格 + 批量 + 交付表）。
连接建模当时明确不在范围内。
