# 使用文档 · ANSA MCP + Skills

一套让 AI 直接操作 ANSA 的工具：**MCP 服务**负责能动手（22 个工具），**Skill** 负责知道怎么动（四个领域工作流）。
两者可以分开用，一起装才是完整链路。

- 适用：Windows + BETA CAE ANSA（实测 24.1.2，23.x 需自行复核）
- 许可：MIT
- 内容：`ansa_mcp_server.py` + 22 个 MCP 工具 + 4 个 Agent Skill + 一批可离线运行的脚本

---

## 0. 这套东西现在能做什么

| 层 | 覆盖 | 不覆盖 |
|---|---|---|
| MCP 工具 | 启停/连接、跑任意 ANSA Python、开存导出、网格、实体查询、异步长任务、截图、**模型快照与差异**、**结构自检** | 不替你做工程判断 |
| `ansa-midsurface-mesh` | 薄壁件抽中面 → 划壳网格 → 批处理 → 交付表 | 体网格 |
| `ansa-bolt-connection` | 螺栓实体 → 2 RBE2 + 1 CBAR(PBARL ROD) | 焊接螺母等组合件 |
| `ansa-bushing-connection` | 橡胶衬套 → 2 RBE2 + 1 CBUSH/PBUSH（零长度或真实长度，K1..K6 自动算） | 橡胶本体的超弹/频变特性 |
| `ansa-seam-weld` | 焊道管 → seam line → **RBE2 / CBAR / 实体 CHEXA+RBE3 / 四边形共节点** 四种表示 | 点焊、胶接、包边（尚未做） |

四个技能各自带 `SKILL.md`（触发词、流程、坑表）和 `scripts/`（可直接 `exec` 的脚本），
细节看技能目录，本文只讲怎么装、怎么验、怎么排错。

---

## 1. 先确认三件事

| 检查项 | 怎么查 | 期望 |
|---|---|---|
| ANSA 安装路径 | 资源管理器里找到 `ansa64.bat` 所在目录 | 形如 `C:\Program Files\BETA_CAE_Systems\ansa_v24.1.2` |
| Python | `python --version` | 3.10+（3.13 实测可用） |
| MCP 客户端 | WorkBuddy / Claude Code / Cursor 任一 | 支持 stdio 型 MCP 服务 |

下文用 `%ANSA_HOME%` 代指 ANSA 安装目录，`%REPO%` 代指本包解压目录，**替换成自己的实际路径**。

---

## 2. 装 MCP 服务

### 2.1 建虚拟环境

```bat
cd /d %REPO%
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` 只有一条：`mcp>=1.2.0`。
国内网络装不上时加镜像：`-i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com`。

### 2.2 写 MCP 配置

WorkBuddy 在 `%USERPROFILE%\.workbuddy\mcp.json`（Claude Code 是 `.mcp.json`，Cursor 是 `%USERPROFILE%\.cursor\mcp.json`）。
合并进 `mcpServers`，**不要覆盖已有服务**：

```json
{
  "mcpServers": {
    "ansa": {
      "command": "%REPO%\\.venv\\Scripts\\python.exe",
      "args": ["%REPO%\\ansa_mcp_server.py"],
      "env": { "ANSA_HOME": "%ANSA_HOME%" }
    }
  }
}
```

`command` 必须是**绝对路径**并双反斜杠转义。

### 2.3 验证

让 agent 调 `ansa_status`：`connected: false` 是正常的（还没启动 ANSA）；
能返回版本号就说明协议层通了。再调 `ansa_launch`，通常 10–20 秒后 `connected: true`
（量级参考 README 的实测性能表：gui 12.8 s、batch 4.0 s）。

### 2.4 多版本切换

先做完 2.2（脚本是读改写已有的 `%USERPROFILE%\.workbuddy\mcp.json`，没有这个文件会报错），然后：

```bash
python tools\switch_ansa.py            # 只列出本机装了哪几个 ANSA
python tools\switch_ansa.py 24.1.2     # 按版本号切
python tools\switch_ansa.py 2          # 或按列表里的序号
```

它只改 `ansa` 服务的 `ANSA_HOME`，其他配置一律保留。用 Claude Code / Cursor 的得手动改
各自配置文件。改完**必须重开 MCP 会话**。

---

## 3. 装 Skill

把四个目录分别复制到 agent 的 skill 目录：

| 技能 | 目标 |
|---|---|
| `skills\ansa-midsurface-mesh\` | `%USERPROFILE%\.workbuddy\skills\ansa-midsurface-mesh\` |
| `skills\ansa-bolt-connection\` | `...\skills\ansa-bolt-connection\` |
| `skills\ansa-seam-weld\` | `...\skills\ansa-seam-weld\` |
| `skills\ansa-bushing-connection\` | `...\skills\ansa-bushing-connection\` |

Claude Code 原生路径是 `%USERPROFILE%\.claude\skills\<name>\`。
`SKILL.md` 的 `description` 会自动触发——说「抽中面划 5mm 网格」「建螺栓连接」「这几根焊缝连起来」「建一个衬套连接」时 agent 会加载对应技能。

装完**新开一次会话**才会出现在技能列表里。

---

## 4. 动手之前：先学会验模型

这三个工具是新增的核心，原因是过去每次改动都靠临时写循环去数，**漏过事**：
某个焊缝模式把 1 971 个板单元悄悄改挂到新属性上，单元总数一个没变，直到有人手写快照才发现。

| 工具 | 作用 | 实测 |
|---|---|---|
| `ansa_snapshot(name)` | 记下单元数、单元→属性、属性表 | 63 338 壳 + 2 252 RBE2，秒级 |
| `ansa_diff(name)` | 与快照比对：增/删/**改属性** | 故意改 3 个壳的属性，精确报出 3、增删均 0 |
| `ansa_verify(checks_wanted)` | 结构自检 | 见下 |

`ansa_verify` 的可选检查：

| 检查 | 内容 | 63 000 壳耗时 |
|---|---|---|
| `counts` | 实体普查（总是执行） | 瞬时 |
| `props` | 没被任何单元使用的属性 | 瞬时 |
| `mats` | 属性 MID 找不到 MAT1（求解器会拒） | 瞬时 |
| `rbe` | RBE2/RBE3 节点数 < 2（焊不上） | 瞬时 |
| `free` | 没被任何单元或刚体引用的节点 | **0.6 s**（65 446 节点，结果 0） |
| `quality` | 零面积壳、长宽比 > 10 | **1.5 s**（零面积 0，长宽比超标 7） |
| `native` | **ANSA 自带 Checks Manager 的 10 项（参考项）** | **2.4 s** |

`native` 跑的是 ANSA 自己定义的检查，按类别分三组，一次返回状态、问题数、出现最多的消息与错误码、涉及的实体 id：

- `general`：自由节点、**连接检查**、未定义材料/属性、重复单元、RBE3 定义
- `mesh`：网格质量、非流形节点
- `penetration`：干涉、**邻近度（零件间互相穿插）**

> **这一组是参考，不是门槛。** ANSA 的判据偏严：参考模型被判 283 个翘曲单元超标、
> 154 条连接问题，而模型一直正常在用。它的价值在于**指出该看哪里、以及拿什么去谈**，
> 不在于判模型能不能用。真正必须在任何口径下都成立的，是 `free` / `mats` / `rbe` 这三项。

连接检查逐条给原因和连接 id，比自己猜“为什么这条焊缝建不出”可靠：

| 错误码 | 消息 | 条数 |
|---|---|---|
| E3881 | Bad total thickness of connecting parts | 84 |
| E3877 | Bad maximum distance between outer parts 1 and 2 | 43 |
| E3880 | Bad connecting flanges angle | 25 |
| E3875 | Connection failed to project on part #1 / #2 | 2 |

网格质量与邻近度的粒度也够用：一项报告里同时给“warping 283 个超标、jacobian 8 个、skewness 15 个”，
邻近度直接指出“哪两个 PID 之间穿插”（E3966/E3967）。

推荐用法：**改之前 `snapshot`，改之后 `diff` + `verify("counts,native")`**。三步都只读，不占额外 license。

---

## 5. 四个技能的最短路径

### 5.1 抽中面 + 划网格

```python
PARAMS = {
    "catpart": r"D:/proj/part.CATPart",
    "engine": "mesh",
    "mpar": r"D:/proj/5MM.ansa_mpar",
    "qual": r"D:/proj/5MM.ansa_qual",
    "max_thickness": 6.0,
    "save": r"D:/out/part.ansa",
    "nas":  r"D:/out/part.nas",
}
import os
exec(open(os.path.expandvars(r"%USERPROFILE%/.workbuddy/skills/ansa-midsurface-mesh/scripts/process_part.py"), encoding="utf-8").read(), globals())
RESULT
```

看三个数：`shells`、`thickness`、`edge_p50`。`edge_p50` 对不上目标尺寸说明 mpar 没生效。

批量（几十件走单会话 `process_folder.py`，几百件走 `parallel_batch.py` 多进程），
交付表用 `make_summary.py`，详见该技能 `SKILL.md`。

### 5.2 螺栓连接

```python
PARAMS.update({
    "stage": "all",
    "bolts": [r"D:/bolts"],                # 螺栓 CATPart 目录（或文件清单）
    "structure": r"D:/out/structure.nas",  # 结构网格模型，stage="all" 必需
    "work_dir": r"D:/out/bolt",
})
exec(open(r".../ansa-bolt-connection/scripts/bolt_connection.py", encoding="utf-8").read(), globals())
```

PARAMS 沿用 5.1 那段。`stage="all"` 会先打开螺栓件、再重新导入 `structure`；
不给 `structure` 会直接抛 `RuntimeError`，`bolts` 也同样必填。

每个螺栓出 2 个 RBE2 + 1 根 CBAR。注意 `PBARL` 的 DIM1 在导入和导出时都会丢，脚本两处都补。

### 5.3 焊缝连接

四种表示，按用途选：

| 表示 | FE 类型 | 用途 | 参考模型建成率 |
|---|---|---|---|
| `RBE2` | 2 节点刚性链接 | 只求整体刚度/模态 | 133 / 133 |
| `CBAR` | `PBARL ROD` 弹性杆 | 想要一点柔性 | 133 / 133（后处理） |
| `RBE3-HEXA-RBE3` | CHEXA 实体砖 + 两侧 RBE3 | 要传力、要看焊缝附近应力 | 132 / 133 |
| `OVERLAP-SHELL` | 共节点 CQUAD4 | 要像真焊缝的四边形连接 | 111 + 22（回退）= 133 |

前提：当前模型里已有 `SeamLine_Type` 连接——先按 SKILL.md §1–§2 跑
`seam_extract.py` → `seam_build.py`；本步只把已有连接改成四边形表示，
模型里没有连接会返回 `connections: 0`（`shell_rep.py` 不建连接）。
脚本默认操作当前已打开的模型；要传 `model` 只能给 `.ansa`/CAD 文件，`base.Open` 不吃 `.nas`。

```python
exec(open(r".../ansa-seam-weld/scripts/shell_rep.py", encoding="utf-8").read(), globals())
PARAMS = {"width": 4.0, "root_shells": "Single Row",
          "restore_properties": True}
run(PARAMS)
```

脚本会自动：设宽度 → 布焊点 → 转单元 → 统计重划/改属性 → 还原板属性 → 列出失败焊缝。

### 5.4 衬套连接

```python
exec(open(r".../ansa-bushing-connection/scripts/bushing_connection.py", encoding="utf-8").read(), globals())
RESULT = run({
    "stage": "all",
    "rubber": [r"D:/parts/06_nonmetal"],   # 橡胶件目录（或文件清单）
    "structure": r"D:/out/structure.nas",  # 结构网格模型，stage="all" 必需
    "mode": "point",                       # "point"（默认，零长度）| "spanning"（真实长度）
    "k_rad": 1000.0,                       # 径向刚度占位值 N/mm，按橡胶硬度改
    "work_dir": r"D:/out/bush",
    "export_nas": r"D:/out/class5_bush.nas",
})
```

同轴的多段橡胶件（胶套 + 轴承）会自动合并成一只衬套。板位、孔缘环、跨距都从网格量，
不用手输；`origin`/`span` 只用于初始搜索。

两种模式**刚度等价**（class5 上解出 165.6 / 9913.9 N/mm，五位数字一致）：
`spanning` 只是把元件拉长到真实板距、网格落在板中面。除非有非得看全长元件的交付规范，
否则用 `point`。

---

## 6. 什么算合格

| 技能 | 门槛 |
|---|---|
| 中面网格 | 单元数接近 `面积/边长²`；`edge_p50` 与目标差 ~6%；`T=1.0` 的 PSHELL 为 0；`MAT1` 存在；无游离小块 |
| 螺栓 | 每螺栓 2 RBE2 + 1 CBAR；CBAR 两端节点有效；`PBARL` 的 `DIM1` 在 deck 里非空 |
| 焊缝 | 连接数量对得上焊道数；每条连接至少两个零件；自由节点 0；RBE2 节点数 ≥ 2；导出的属性材料闭环 |
| 衬套 | 每衬套 2 RBE2 + 1 CBUSH；`PBUSH` 的 K1..K6 回读非零且顺序为 轴向/径向/径向/扭转/摇摆/摇摆；`point` 与 `spanning` 两种建法解出的位移一致 |

**汇报时把三件事分开说**：连接存在、单元真的生成了、接头工程有效——第三件没有任何自动步骤能断言。

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: mcp` | 用了系统 Python | `command` 指向 `.venv\Scripts\python.exe` |
| `ansa_status` 找不到 ANSA | `ANSA_HOME` 没设或指错 | 确认目录下有 `ansa64.bat` |
| 启动就崩 / 中文路径 | ANSA 读不了非 ASCII 路径 | 模型和输出都放纯英文目录 |
| `RealizeConnections` 报成功但没元素 | 它不校验 FE 名称，未知名字静默回落 RBE2 | 看元素计数，不看返回值 |
| 焊缝实体模式返回 0 | 连接卡 `W`（宽度）默认 0 | 先设 `W = 焊道直径` |
| 两次相同的运行结果不同 | `SetDefault` 是会话级，`session.New` 不清 | 显式设值，或用 `""` 清空 |
| 扫多个模式数字越来越大 | `EraseConnectionFeRep` 不回滚重建的母材壳 | 一模式一会话 |
| 焊缝属性把板单元改了 | 模式会把焊缝区板单元改挂到新属性 | `snapshot` 后 `restore_properties=True` |
| 复开 `.ansa` 后 `CollectEntities(..., "SHELL")` 返回 0 | 复开后关联丢失 | 改从单元卡 `PID` 反查 |
| 批量跑一半 worker 全死 | 输出目录旧文件被批量删除撞上删除守护 | 换新目录，`os.replace` 回填 |

更多一手记录见各技能 `references/`。

---

## 8. 自检

```bash
.venv\Scripts\python.exe tests\test_ledger.py    # 离线 35 项
.venv\Scripts\python.exe tests\test_checks.py    # 离线 25 项，快照/差异/自检的代码契约
.venv\Scripts\python.exe tests\test_mcp.py       # MCP stdio 契约
.venv\Scripts\python.exe tests\test_server.py batch     # 会启动 ANSA（batch），占 license
.venv\Scripts\python.exe tests\test_gui_tools.py        # 会启动 ANSA（gui），占 license
```

前三条不占 license，装完先跑它们。后两条各自启动一个实例，跑完自动退出。

---

## 9. 边界

**不做**：四面体/体网格、已有壳网格重划、求解与结果评估。

**连接只做了两类**：螺栓、缝焊。点焊（`SpotweldPoint_Type`）、胶接（`AdhesiveLine_Type`）、
包边（`Hemming_Type`）尚未实现——它们的接口与缝焊同源，但没验证过，不要假设能用。

**`ansa_api_search`** 依赖 `tools\ansa_api_index.json`。这个文件不进 git（体积大、随安装版本变化），
新克隆的仓库里没有，第一次用之前先生成：

```bash
python tools\build_api_index.py                          # 自动找本机 ANSA 安装
python tools\build_api_index.py --ansa-home "%ANSA_HOME%"
```

索引也可以放在别处，用环境变量 `ANSA_API_INDEX` 指向它。随包分发的索引按 ANSA 23.1.1
文档生成（5 689 条函数）；装的不是这个版本就重建，索引不匹配时搜出来的签名会骗人。

---

## 10. 版本与变更

版本号就是日期，当前 **2026-11-26**，以 `CHANGELOG.md` 顶部一节为准。
README / USAGE 不自带版本号，避免三处对不上。
