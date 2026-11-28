# 变更记录

按时间倒序。每条只写「改了什么、为什么」，不写「优化了体验」这类空话。

**当前版本：2026-11-28**。版本号就是日期，以本文件最上面的一节为准；
README / USAGE 不再各自写版本号，改动到这里就同步。

## 2026-11-28

### 改进：`organize_output.py` 分出"有疑问"与连接参考目录

原来的整理脚本只按「状态」列分 6 类，"已出网格"和"出的网格能用"混在一起：
A0072600194 两个实例各裂成 2 块、边长偏离目标 41%/54%，却和 380 个合格件一起躺在 `02_Skin成功`。

- 新增 `07_有疑问_需复核`：`done` 但连通块 >1 或 `校验` 不通过 → 进 07，不进 02；交付数不再掩盖它们
- 新增 `08_焊点` / `09_焊缝` / `10_粘胶`：`--refs` 把 R*_WELD / R*_ADHESIVE 参考件**硬链接**过去（几十 MB 一件，不复制），
  `--beads` 从焊接体扫描结果里挑 >=20 mm 的长条体写 `焊缝候选_清单.csv`
- `06_玻璃_未纳入` 改名 `06_非钣金_未纳入`（里面还有泡沫、门槛嵌入件、吸能盒，不只是玻璃）
- 加上 `00_总表`，README 的件数改为取本次实际值；对账口径按汇总表实际记载的文件数，不再假设每件都是 ansa+nas
- `skin_failed` 明确映射到 `05_最终失败`（原来只在 `SKIN_FAILED` 元组里）

BIW 553 引用实测：540 件 → 00(6) / 01(129) / 02(380) / 03(1) / 05(12) / 06(19) / 07(5) / 08(13) / 09(2) / 10(2) / _工作目录(582)。
13 个 R 参考件的几何扫描（`out/134_weld_beads.py`）：CATPart 只有 FACE 没有 SOLID，按面 bbox 2 mm 网格 + 0.35 mm 容差聚类出
6 288 个焊接体，P50 = 8.0 mm（点焊 nugget），>=20 mm 的 599 条列为焊缝候选——**只是候选**，8 mm 焊点被容差连成链也会变长。

### 新增：`ansa-bushing-connection` 技能（橡胶衬套 → CBUSH/PBUSH）

class5 的 6 个橡胶件（橡胶轴承 / 胶套 / 衬套）此前没有 FE 表示：照螺栓建 CBAR 会当成刚性杆，
照焊缝建壳又与橡胶的作用相反。改为一只 CBUSH + 一张 PBUSH + 每块板一片 RBE2：

- `stage: extract` 从橡胶 CATPart/STEP 提轴线，同轴多段件自动合并（class5：6 件 → 2 只衬套）
- `stage: build` 沿轴线做 1 mm 直方图找两块板的 z 位置，再取孔缘环节点——跨距、环半径、辐条数都不用输
- `PBUSH` 顺序固定 `[轴向, 径向, 径向, 扭转, 摇摆, 摇摆]`；K1 = E·A/L，K4 = G·J/L，K5 = K6 = k_rad·L²/12
- `mode`: `point`（2 mm 元件在跨距中点）与 `spanning`（元件长度 = 真实板距，网格落板中面）

class5 实测：BUSH_1 K = [236.1, 1000, 1000, 27 429.8, 161 333.3, 161 333.3]，
BUSH_2 K = [432.9, 1000, 1000, 49 594.7, 44 246.6, 44 246.6]；1000 N 下单接头 +Z 165.6 N/mm、+Y 9 913.9 N/mm。
橡胶本体不进 deck，孔缘由 RBE2 刚化——要看孔边应力得换 RBE3 或实体橡胶，SKILL.md 里写明了。

### 修正：CBUSH 不是梁，元件长度不改变刚度（推翻初版的 4 倍补偿）

初版照着"真实长度会多出 (L/2)²/K5 杠杆项、面内刚度从 1000 掉到 250、K2 要乘 4"写。
用同一 PBUSH、只改元件长度与网格落点对撞：`point` 与 `spanning` 的位移**逐位相同**（6.0369 mm / 0.1009 mm）。

OptiStruct 的 CBUSH 只把 GA、GB 各 6 个自由度用对角 PBUSH 相连，没有梁式长度耦合，
K2 本身就是板对板径向刚度。`spanning_fix: "scale_radial"` 已删除（传了只告警并回落到物理 K）；
两处文档把旧结论标成 corrected claim，免得后来的人再推一遍。刚性摇摆（`rigid_rocking`）会让整体偏硬 21%，
保留但默认关闭。

### 修复：ANSA 导出把 9 字符的 PBUSH 值挤进相邻列，K5/K6 消失

`OutputNastran` 按 8 列定宽重排 PBUSH，`27429.8` 这种 9 字符值右溢，导出的卡变成
`...  27429.8161333.3161333.3`：K4 与 K5 粘成一个数、K6 丢失。求解器照读不报错，接头静默变软
（同一模型 +Y 刚度 8 525.9 而非 9 913.9，差 14%）。技能改为导出后把所有本计划的 PBUSH
重写成自由字段（`PBUSH,pid,K,k1..k6`），回读精确到 6 位有效数字。

### 新增：`partial` 台账状态 + `MCP_PRECONDITION:` 前置条件标记

之前脚本抛异常时一律记 `failed`，允许原 id 重跑。可"脚本跑到一半改了模型再抛"也被算成 failed，
重跑就把同一处改动做了两遍。现在按实体计数判定：

- 有 delta → 记 `partial`，重跑直接拒绝（`OUTCOME_UNKNOWN`，提示先用 `ansa_diff` 看，再换新 id）
- 无 delta → 仍记 `failed`，可重跑
- 脚本自己声明 `RuntimeError("MCP_PRECONDITION: ...")`（参数不全、前置文件缺失）→ 记 `failed`，
  因为可以确定它没碰模型

顺带：ANSA 回了消息但没给 result code 时不再算 `SCRIPT_ERROR`——那可能已经跑了，改记 `unknown`。

### 修复：IAP 连接不是线程安全的

一个 TCP 流上同时只能有一个请求/应答。两个线程各自写帧会把对方的应答吃掉：
成功的写入被报成脚本错误，或者旧应答被当成新调用的结果。现在每次交换持锁，
并校验应答帧的事务号与请求一致，不一致直接 `OUTCOME_UNKNOWN`（不重试）。
`ansa_status` 遇到"连接被占用"不再插探测帧——占用不等于掉线，新增返回 `busy`。

### 修复：批量抢救清单每进程覆盖（`casting_midsurf.py`）

`casting_batch.py` 一件一个 ANSA 进程，每个进程只看到自己那一行，却用 `"w"` 打开
`抢救清单.csv`：12 件跑完只剩最后 1 行，不报错。改为合并——保留不是本进程处理的行，再追加自己的。

### 修复：技能脚本不再内置参考模型的路径

7 个脚本（`seam_build` / `seam_chunks` / `seam_extract` / `seam_final` / `rbe2_to_bar` /
`shell_rep` / `solid_rep`）原来带一份 `TPARAMS = {"nas_dir": "D:/out/parts", ...}`，
裸 `exec()` 会去跑参考模型的目录还报告成功。现在没有 `PARAMS` 就抛
`MCP_PRECONDITION: ... PARAMS is empty`，缺必填键也点名缺哪个。

### 新增：可执行基线（`tests/baselines/`）

把两次实测钉成回归基线：`midsurface_540.json`（文档推导，7 项）与 `gemo_95.json`
（本机实测，10 项）——零件/壳数、Casting 轮次、五金件分类、交付 deck 里的 PSHELL 厚度、
共享输出的门槛值。`--selftest` 用 18 项证明门槛真的会在漂移时报警。

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
