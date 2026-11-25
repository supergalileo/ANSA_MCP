# 变更记录

按时间倒序。每条只写「改了什么、为什么」，不写「优化了体验」这类空话。

**当前版本：2026-11-26**。版本号就是日期，以本文件最上面的一节为准；
README / USAGE 不再各自写版本号，改动到这里就同步。

## 2026-11-26

### 修复：`ansa_api_search` 找不到索引时把人指向一个 404 地址

原文案教用户 `pip install git+https://github.com/<ansa-api-mcp>`——那是个占位符，
照做必然 404；而索引被 `.gitignore` 排除，新克隆的仓库里恰好就是没有，
所以新人最先撞到的错误提示本身就是错的。

改为指向 `python tools/build_api_index.py`（自动探测失败时加 `--ansa-home <安装目录>`），
并点明索引由 `.gitignore` 排除、新克隆必然没有，也可以设 `ANSA_API_INDEX` 指到已有的 JSON。
只改这一处返回文案，索引查找顺序没动。

### 文档：README / USAGE 与实现对齐

一次性修掉对外描述里已经和代码脱节的地方：

- README 工作原理图写 `15 tools`，实际 22（工具表也补上了 `ansa_snapshot` / `ansa_diff` / `ansa_verify`）
- README 说 API 索引取自 24.1.2 安装目录，实际随包的索引是 `ansa_v23.1.1`、5 689 条函数
- README「已知限制」还写着**不做连接建模**，且链到 `docs/交接_连接能力_2026-11-09.md`——
  该文件被 `.gitignore` 排除、仓库里不存在，是死链；改为说明连接只做了螺栓与缝焊
- README 目录表漏了 `split_step.py`、两个 casting 脚本、`rescue_midsurf.py`、`organize_output.py`、
  `inspect_model.py`；测试段漏了 `test_checks.py` 与 `test_gui_tools.py`
- USAGE 自检清单补 `test_gui_tools.py`；写明 `tools/ansa_api_index.json` 不进 git、首次要先生成；
  写明 `switch_ansa.py` 只改 `~/.workbuddy/mcp.json`、且要先有该文件

版本口径统一为日期制，README / USAGE 不再各自写版本号。

## 2026-11-22

### 修复：焊缝四边形导出的 MAT1 悬空（`shell_rep.py`）

class5 全量跑通后用 `ansa_verify` 的 native 组验收，暴露一个会静默出错的问题：
模型里 50 个属性都有材料（MAT1 1–28 齐全），但 `OutputNastran` 只写原有的 22 个 MAT1，
7 个焊缝属性的 MID 悬空。求解器要么报错，要么悄悄用默认材料。

- `patch_materials()` 同时扫 `PSHELL` 与 `PSOLID` 的 MID，缺失的追加 MAT1，
  新卡用默认材料 210000 / 0.3 / 7.85e-9；修补后材料闭环：28 张 MAT1 对 28 个 MID
- 与实体焊缝同一个坑，两种表示都会丢；技能文档里已并列记录

class5 结果：**连接覆盖 133/133**——111 条共节点 quad + 22 条 `RBE3-SHELL-RBE3` 回退，
133 条连接都拿到了 FE 表示。注意这只是覆盖数，材料闭环是上面那件事，两者分开看。
同一轮 `ansa_diff`：SHELL +2 560、RBE2 2 252→86、RBE3 0→300、属性 22→50、repropertied 0（属性还原生效）。

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

## 2026-11-19

### 新增：装配 STEP 拆分为单零件并分类（`scripts/split_step.py`）

整车装配往往是一个 STEP 塞几百个零件，原流程只吃单件。新增纯 Python 拆分器，
不启动 ANSA、不占 license：

- 解析 AP203/AP214，按 `PRODUCT` 或 `MANIFOLD_SOLID_BREP` 拆分
- 装配变换累积后烘焙进坐标，零件落在实际位置
- 按 solid 名称（CATIA X2 UTF-16BE 转义）分类：钣金件 / 紧固件 / 焊缝圆管 / 非金属 / 焊点
- 混合件（钣金 + 焊接圆管）默认按实体自动拆开
- `parallel_batch.py` / `process_folder.py` 放开扩展名限制，支持
  `.stp/.step/.igs/.x_t/.jt`，拆分结果直接进原流程

实测 51 MB 尾门装配：面数与源文件一致（7 800），分类出 143 个焊缝实体（127 个在 CATIA 里就叫 welding）。

三个坑写进 `references/step-splitting.md`：`VECTOR` 参数是 `(name,#direction,magnitude)`，
当坐标改写会崩；`SHAPE_REPRESENTATION_RELATIONSHIP` 入度为 0，漏掉后导入 0 面且无报错；
bbox 污染源是 `AXIS2_PLACEMENT_3D` 原点，且圆柱直径不在任何顶点上。

## 2026-11-17

### 新增：`skills/ansa-bolt-connection`（螺栓连接技能）

从螺栓 CATPart 提取轴线与杆径，为每个螺栓建 2 RBE2 + 1 CBAR（`PBARL ROD`）：

- 孔心取自 ANSA 孔识别再吸附到螺栓轴线，不从孔边节点取
- 从节点按孔半径 + 5 mm 抓整圈，用角度缺口 ≤45° 判完整性
- 多层板合并到同一片 RBE2；单层板（焊接螺栓/盲孔）只建 1 片、不建梁
- 两阶段 `extract` / `build`；导出后自动补 `PBARL` 的 `DIM1`（ANSA 双向丢该字段）

含 `references/api-verified.md`（验证过的 API 签名 + 18 螺栓实测基准）。

## 更早

2026-11-17 之前的版本只有 `ansa-midsurface-mesh`（抽中面 + 划网格 + 批量 + 交付表），
连接建模当时明确不在范围内。
