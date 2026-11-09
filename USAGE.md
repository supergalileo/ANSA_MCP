# 使用文档 · ANSA MCP + ansa-midsurface-mesh Skill

两步装好：**装 MCP 服务**（让 AI 能操作 ANSA）、**装 Skill**（让 AI 知道怎么抽中面划网格）。
只用其中一个也能工作，两个都装才是一套完整流程。

- 适用：Windows + BETA CAE ANSA（实测 24.1.2，23.x 应可用，需自行复核）
- 许可：MIT
- 内容：`ansa_mcp_server.py` + 19 个 MCP 工具 + 1 个 Agent Skill + 一批可独立运行的批处理脚本

---

## 0. 先确认三件事

| 检查项 | 怎么查 | 期望 |
|---|---|---|
| ANSA 安装路径 | 资源管理器里找到 `ansa64.bat` 所在目录 | 形如 `C:\Program Files\BETA_CAE_Systems\ansa_v24.1.2` |
| Python | `python --version` | 3.10+（3.13 实测可用） |
| MCP 客户端 | WorkBuddy / Claude Code / Cursor 任一 | 支持 stdio 型 MCP 服务 |

下文用 `%ANSA_HOME%` 代指 ANSA 安装目录，`%REPO%` 代指本包解压目录，**替换成你自己的实际路径**。

---

## 1. 装 MCP 服务

### 1.1 建虚拟环境并安装依赖

```bash
cd /d %REPO%
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` 只有一条：`mcp>=1.2.0`。

> 国内网络装不上时加镜像：`pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com`

### 1.2 写 MCP 配置

WorkBuddy 的配置在 `%USERPROFILE%\.workbuddy\mcp.json`（Claude Code 是 `.mcp.json`，Cursor 是 `%USERPROFILE%\.cursor\mcp.json`）。
把下面这段合并进 `mcpServers`，**不要覆盖已有的其他服务**：

```json
{
  "mcpServers": {
    "ansa": {
      "command": "%REPO%\\.venv\\Scripts\\python.exe",
      "args": ["%REPO%\\ansa_mcp_server.py"],
      "env": {
        "ANSA_HOME": "%ANSA_HOME%"
      }
    }
  }
}
```

注意：

- `command` 必须写**绝对路径**，且用双反斜杠转义。
- JSON 里不认 `%REPO%`，把占位符替换成真实路径后再保存。
- `ANSA_HOME` **必填**。服务端代码里的默认值是别人的安装路径，不设会启动失败。
- 路径含空格没问题（用 JSON 字符串包住即可），但**含中文的路径风险高**，ANSA 侧可能读不到，尽量放在纯 ASCII 目录下。

### 1.3 验证

重启 MCP 会话（大多数客户端需要重开窗口），然后在对话里问一句：

```
ansa_status
```

期望返回 `connected: false`、`ansa_home` 指向你的目录、版本号正确。
`connected: false` 是**正常的**——服务还没启动 ANSA 实例，这不代表装坏了。

再让它启动：

```
ansa_launch mode=gui
ansa_status
```

这次应返回 `connected: true` 和 ANSA 版本号。

### 1.4 环境变量（可选）

| 变量 | 默认 | 用途 |
|---|---|---|
| `ANSA_HOME` | 代码内兜底路径 | ANSA 安装目录，**务必显式设置** |
| `ANSA_BAT` | 自动探测 | 直接指定 `ansa64.bat`，多版本并存时用 |
| `ANSA_MCP_TIMEOUT` | `600` | 单次脚本执行超时（秒） |
| `ANSA_MCP_RESULT_LIMIT` | `120000` | 返回结果最大字符数 |
| `ANSA_MCP_WORKSPACE` | 未设置 | 设了以后所有写出路径必须落在此目录内，防误写 |
| `ANSA_API_INDEX` | 自动查找 | ANSA API 索引 JSON 路径 |

### 1.5 多版本切换（可选）

```bash
python tools\switch_ansa.py            # 列出本机所有 ANSA 安装
python tools\switch_ansa.py 24.1.2     # 切到该版本，改的是 mcp.json 的 ANSA_HOME
```

改完**必须重开 MCP 会话**才生效。

---

## 2. 装 Skill

把 `skills\ansa-midsurface-mesh\` **整个目录**复制到 agent 的 skill 目录：

| 客户端 | 目标路径 |
|---|---|
| WorkBuddy / Claude Code | `%USERPROFILE%\.workbuddy\skills\ansa-midsurface-mesh\` |
| Claude Code（原生） | `%USERPROFILE%\.claude\skills\ansa-midsurface-mesh\` |

复制完目录结构应是：

```
.workbuddy\skills\ansa-midsurface-mesh\
├── SKILL.md
├── references\
│   ├── ansa-api-verified.md
│   └── field-notes.md
└── scripts\
    ├── process_part.py        单件全流程
    ├── process_folder.py      目录批量
    ├── parallel_batch.py      多进程批量（几百件以上）
    ├── casting_midsurf.py     Casting 二遍救援
    ├── casting_batch.py       同上，一件一进程
    ├── make_summary.py        出交付表
    ├── deck_stats.py          网格统计
    ├── mesh_engine.py         两条网格路线
    ├── rescue_midsurf.py      MidSurfAuto 兜底
    └── inspect_model.py       只读勘察
```

Skill 的 `description` 会自动触发——你说「抽中面并划 5mm 网格」时 agent 会自己加载它。

---

## 3. 最快跑通一件

准备：一个闭合薄壁 CATPart、一份 `.ansa_mpar`（网格参数）、一份 `.ansa_qual`（质量标准）。
包里没带这两个文件，它们是项目资产，问你的 CAE 同事要。

在对话里让 agent 执行（或直接在 ANSA 里跑）：

```python
PARAMS = {
    "catpart": r"D:/proj/B0029_A0072600089.CATPart",
    "engine": "mesh",
    "mpar": r"D:/proj/5MM.ansa_mpar",
    "qual": r"D:/proj/5MM.ansa_qual",
    "max_thickness": 6.0,
    "save": r"D:/out/A0072600089.ansa",
    "nas":  r"D:/out/A0072600089.nas",
}
exec(open(r"D:/skills/ansa-midsurface-mesh/scripts/process_part.py", encoding="utf-8").read(), globals())
RESULT
```

`RESULT` 里看三个数：`shells`（单元数）、`thickness`（实测壁厚）、`edge_p50`（实际边长中位数）。
`edge_p50` 要对得上你要的目标尺寸，对不上说明 mpar 没生效。

---

## 4. 批量跑一整个目录

### 4.1 几十件：单会话 `process_folder.py`

`process_folder.py` 没有命令行入口，它是给 ANSA 内部 `exec` 用的。走 MCP 让 agent 执行：

```python
PARAMS = {
    "folder": r"D:/proj/parts", "out_dir": r"D:/out",
    "engine": "mesh",
    "mpar": r"D:/proj/5MM.ansa_mpar", "qual": r"D:/proj/5MM.ansa_qual",
    "nas": True, "limit": 25, "resume": True,
}
exec(open(r"D:/skills/ansa-midsurface-mesh/scripts/process_folder.py", encoding="utf-8").read(), globals())
RESULT["summary"]["issues"]      # 必须看，不能只看成功数
```

中断了用同一份 `PARAMS` 再跑一次即可续跑（读 `_progress.jsonl`）。`limit` 是本次上限，0 = 不限。

### 4.2 几百件：多进程

```bash
python <skill>\scripts\parallel_batch.py --src D:/proj/parts --out D:/out \
    --mpar 5MM.ansa_mpar --qual 5MM.ansa_qual --workers 6
```

- 找不到 `ansa64.bat` 时加 `--ana-bat "C:\...\ansa64.bat"`，或设 `ANSA_BAT` 环境变量。
- 每个 worker 约占 0.4–0.9 GB 内存；16 核 / 64 GB 开 6 路稳定。**并发数受 license 槽位限制**，先问清有几个。
- 这个驱动**故意不走 MCP**：ANSA 内核在某些几何上会直接崩，走 MCP 会把服务端一起带走，每次崩溃都要手动重连。

### 4.3 跑完出交付表

```bash
python <skill>\scripts\make_summary.py --out D:/out --target 5.0 --connectivity --casting D:/out/casting
```

产出四张 CSV：`汇总.csv`、`交付清单.csv`、`未解决清单.csv`、`校验不通过.csv`。

### 4.4 二遍救援 + 网格统计

```bash
python <skill>\scripts\casting_batch.py --src D:/hard --out D:/out/casting \
    --mpar 5MM.ansa_mpar --qual 5MM.ansa_qual --tol 2.0 --timeout 240

python <skill>\scripts\deck_stats.py D:/out/A0072600089.nas --blocks
```

---

## 5. 结果怎么算合格

`make_summary.py` 会自动卡这几道门，全部通过才算交付：

| 门槛 | 期望 |
|---|---|
| 壳单元数 | 非零，且接近 `面积 / 目标边长²` |
| `edge_p50` | 与目标差 ~6% 内；差 20%+ 说明目标尺寸没生效 |
| 每件 `T` | 等于实测值且唯一 |
| `T = 1.0` 的 PSHELL | 必须为 0（说明壁厚没写进去） |
| `.nas` 里的 `MAT1` | 必须存在（否则求解器拿到空材料） |
| 两行共用同一输出路径 | 必须为 0（否则有文件被静默覆盖） |
| 游离小块（≤20 单元或 <1% 面积） | 必须清掉（否则刚度矩阵奇异） |

**汇报时把三件事分开说**：网格存在、壁厚是实测的、模型工程有效——第三件没有自动步骤能断言。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: mcp` | 用了系统 Python 而不是 venv | 配置里的 `command` 指向 `.venv\Scripts\python.exe` |
| `ansa_status` 报找不到 ANSA | `ANSA_HOME` 没设或指错 | 手工确认该目录下有 `ansa64.bat` |
| 启动就崩，日志里有中文路径 | ANSA 读不了非 ASCII 路径 | 把模型和输出都放到纯英文目录 |
| `-execscript "file.py\|main()"` 不执行 | `cmd.exe` 把 `\|` 吃掉了 | 改用 `-exec "load_script: 'file.py'"` |
| Skin 全返回 0，件看着像实体 | 有壁厚超过 `max_thickness`，超了直接返回 0 | 把 `max_thickness` 提到 4.0–6.0，再走 Casting 二遍 |
| 网格边长和目标差很多 | mpar 没生效 / 用了不该用的引擎 | 只允许 `mesh.Mesh` 和 Batch Mesh 出单元 |
| 批量跑一半 worker 全死、无 traceback | 输出目录里旧文件被批量删除，撞上宿主的删除守护 | 换新目录跑，`os.replace` 回填 |
| 交付表行数对，磁盘文件数不对 | 件号重名，输出互相覆盖 | 用本包的 `parallel_batch.py`，它按整个 `--src` 算名称表 |
| 复开 `.ansa` 后 `CollectEntities(..., "SHELL")` 返回 0 | 复开后关联丢失 | 改从单元卡的 `PID` 反查 |

更多一手实测记录见 `skills\ansa-midsurface-mesh\references\field-notes.md`，
验证过的 API 签名与走不通的路见 `references\ansa-api-verified.md`。

---

## 7. 自检

```bash
.venv\Scripts\python.exe tests\test_ledger.py   # 离线，35 项，不占 license
.venv\Scripts\python.exe tests\test_mcp.py      # MCP stdio 契约，不占 license
.venv\Scripts\python.exe tests\test_server.py batch   # 会启动 ANSA，占 license
```

装完先跑前两条，能过就说明 Python 环境和 MCP 协议层没问题。

---

## 8. 边界

本包**不做**这些事，别指望它：

- 四面体/体网格
- 已有壳网格的重新划分
- 连接建模：各零件独立划网格，**节点不共享**，焊点 / tie / rigid 得另外定义
- 一键求解或结果评估

`ansa_api_search` 依赖 `tools\ansa_api_index.json`，那是按 ANSA 23.1.1 文档生成的。
你自己的 ANSA 版本不同就用 `python tools\build_api_index.py --ansa-home <你的安装目录>` 重建一份。
