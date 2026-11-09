# ANSA MCP + Agent Skill

用 MCP 客户端（WorkBuddy / Claude Code / Cursor / Codex）直接驱动 **BETA CAE ANSA** 干活：
查模型、划网格、抽中面、赋厚度、截图、导出求解器文件、执行任意 ANSA Python。

附带一个可复用的 Agent Skill：**从薄壁 CAD 几何自动抽中面、按目标尺寸划壳网格、回写实测壁厚**。

> **English summary.** An MCP server that drives a live BETA CAE ANSA session over the
> official Inter ANSA Protocol, plus a reusable agent skill for mid-surface extraction
> and shell meshing. The server adds idempotent write operations (`operation_id`),
> an explicit `OUTCOME_UNKNOWN` state for lost responses, and layered evidence so a
> caller can tell "the API returned" apart from "the model actually changed".
> ANSA 23.0.0, Windows, MIT.

---

## 目录

| 路径 | 内容 |
|---|---|
| `ansa_mcp_server.py` | FastMCP 服务，19 个工具 |
| `ansa_iap.py` | IAP 协议实现（重写了官方参考实现） |
| `ansa_ledger.py` | 写操作幂等台账 |
| `tests/` | 三层测试，见 [tests/README.md](tests/README.md) |
| `tools/switch_ansa.py` | 列出本机 ANSA 安装并切换 MCP 配置里的 `ANSA_HOME` |
| `tools/build_api_index.py` | 从 ANSA 自带 Sphinx 文档生成 `ansa_api_search` 索引 |
| `skills/ansa-midsurface-mesh/` | 抽中面 + 划网格 Skill |
| `skills/ansa-midsurface-mesh/scripts/process_part.py` | 单件全流程：钣金判定 → 修复 → `Skin` → property 命名 → 划网格 → 保存 |
| `skills/ansa-midsurface-mesh/scripts/process_folder.py` | 目录批量：`_progress.jsonl` 断点续跑、分块、并行 worker、异常清单 |
| `skills/ansa-midsurface-mesh/scripts/parallel_batch.py` | 多进程批量驱动（绕开 MCP，几百件以上用） |
| `skills/ansa-midsurface-mesh/scripts/make_summary.py` | 跑完出四张交付表 + 质检门 |
| `skills/ansa-midsurface-mesh/scripts/deck_stats.py` | 直接从 `.nas` 算 quad 比、自由边、分块 |
| `skills/ansa-midsurface-mesh/scripts/mesh_engine.py` | 两条网格路线（`mesh.Mesh` / Batch Mesh），由 `.ansa_mpar` + `.ansa_qual` 驱动 |

## 工作原理

```
MCP 客户端 (stdio)
      │
      ▼
ansa_mcp_server.py ── FastMCP · 15 tools · 幂等台账
      │
      │ TCP 127.0.0.1:<port>，IAP 线协议
      ▼
ANSA 进程（以 -listenport 启动）
      │ 在自己的进程内执行脚本
      ▼
ansa.base / ansa.mesh / … → 真实的 ANSA 数据库
```

用的是 ANSA **官方**远程控制协议 IAP，不是模拟鼠标点击。脚本在 ANSA 进程内执行，
拿到的是真正的数据库对象，不是截图识别。

ANSA 官方参考实现（`<ANSA>/scripts/RemoteControl/`）有三个问题，本项目重写了协议层：

1. `recv()` 按 `length` 而非 `length-16` 读，且不处理 TCP 分片 → 大响应会损坏；
2. 字符串 TLV 解码引用了未定义变量 `length` → 直接抛异常；
3. 无握手重试，而 ANSA 先开端口、几秒后协议栈才就绪 → GUI 模式必失败（WinError 10054）。

### 两个关键实现细节

- **`print` 捕获**：ANSA 把 `sys.stdout` 接到自己的控制台，`redirect_stdout` 无效。
  服务端改为向执行命名空间注入私有 `print`，输出随结果回传。
- **跨调用状态**：每次调用都是全新脚本，但数据库是持久的。需要跨调用保存 Python 对象时
  用 `ansavars`（存在 `sys.modules` 里的一个字典）。

## 安装

**逐步安装说明见 [USAGE.md](USAGE.md)**（含 MCP 配置、Skill 安装、常见问题）。下面是速查：

```bash
pip install -r requirements.txt
```

写入 MCP 客户端配置（WorkBuddy 是 `~/.workbuddy/mcp.json`）：

```json
{
  "mcpServers": {
    "ansa": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["D:\\path\\to\\ansa_mcp_server.py"],
      "env": {
        "ANSA_HOME": "D:\\Program Files\\BETA_CAE_Systems\\ansa_v23.0.0"
      }
    }
  }
}
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANSA_HOME` | 编译期兜底 `C:\Program Files\BETA_CAE_Systems\ansa_v23.0.0` | ANSA 安装目录，**每台机器必须显式设置** |
| `ANSA_MCP_TIMEOUT` | `600` | 单次脚本执行的默认超时（秒） |
| `ANSA_MCP_RESULT_LIMIT` | `120000` | 返回结果的最大字符数 |
| `ANSA_MCP_WORKSPACE` | 未设置 | 设置后所有写出路径必须落在该目录内 |
| `ANSA_API_INDEX` | 自动查找 | ANSA API 索引 JSON，供 `ansa_api_search` 使用 |

## 工具

| 工具 | 用途 |
|---|---|
| `ansa_status` | 连接状态、版本、模型统计、工作区、未决操作数（不会启动 ANSA） |
| `ansa_launch` | 启动 ANSA（`gui` / `batch`），可顺带导入文件 |
| `ansa_attach` | 接管一个自己手动启动的 ANSA（需带 `-listenport`） |
| `ansa_shutdown` | 退出本服务启动的实例；外部实例只断开不退出 |
| `ansa_open` | 导入文件，按扩展名自动选导入器；`merge=false` 需 `confirm=true` |
| `ansa_save` | 保存 `.ansa`；目标已存在需 `overwrite=true` |
| `ansa_export` | 导出求解器文件；目标已存在需 `overwrite=true` |
| `ansa_model_info` | 模型统计；`detail=true` 按类型聚合属性/材料/零件 |
| `ansa_list_entities` | 列出某类实体的 id / 名称 / 指定字段 |
| `ansa_get_entity_values` | 读单个实体的完整编辑卡 |
| `ansa_mesh` | 划网格（`batch` 批处理生成器，或 free/best/map） |
| `ansa_screenshot` | 抓图：`target="viewport"` 只拍 3D 视口，`target="window"` 拍整个界面（菜单栏 / 工具栏 / 模型树 / 侧栏 / 命令行） |
| `ansa_gui_state` | 当前模块菜单、是否真是 gui 模式、当前 deck、所有可见窗口标题与尺寸 |
| `ansa_gui_menu` | 读或切换模块菜单（TOPO / MESH / VOLUME MESH / MORPH / HEXA BLOCK / CROSS / KINETICS） |
| `ansa_run_python` | **万能入口**：在 ANSA 里跑任意 Python |
| `ansa_run_async` | 后台线程派发长任务，立即返回 `job_id`，`print()` 实时落盘 |
| `ansa_job_status` | 查 job 状态并 tail 实时日志 |
| `ansa_step_history` | 最近 100 步的序列 / 状态 / 数量差值 |
| `ansa_api_search` | 查 ANSA Python API 签名和示例 |

### GUI 相关能力

ANSA 没有 HyperMesh 那种可注入的 Tcl 监听器，它的 IAP 本身就是"在可见 GUI 会话里执行
Python"，所以不需要额外 bridge。只有三件事需要专门处理：

| 需求 | 做法 | 限制 |
|---|---|---|
| 看几何 | `utils.SnapShot()` | 只拍 3D 视口（实测 1089×400） |
| 看整个界面 | `PrintWindow(PW_RENDERFULLCONTENT)` + GDI + 自写 PNG 编码 | 实测 3697×1346、98.6% 非黑；`utils.SnapShotWindow()` 要的是 ANSA 内部 BC 窗口名，主窗口取不到 |
| 长任务不卡客户端 | `ansa_run_async` 用 server 侧工作线程发包 | ANSA 侧仍是同步执行，GUI 会冻结，除非循环里调 `pump()` |

`pump()` 注入在 job 命名空间里，等价于 `guitk.BCApplicationProcessNonUserInputEvents()`：
ANSA 跑 IAP 脚本期间自己的事件循环不转，窗口不刷新，循环里定期调它才能看到进度。

> **为什么不用定时器排队**：`guitk.BCTimerSingleShot()` 从 IAP 脚本里注册，脚本一返回
> 定时器就不再触发（实测 job 永远卡在 `queued`，只有别处调用
> `BCApplicationProcessNonUserInputEvents()` 时才补跑）。HyperMesh MCP 的 `after 1`
> 能成立是因为 Tcl 解释器常驻；Python 侧没有等价物，延迟只能放在 MCP 端。

GUI 工具不模拟鼠标键盘，也不遍历菜单控件——所有操作走 Python API。

### `ansa_open` 的导入器路由

`base.Open` **只认 CAD 格式**，喂它 `.nas` 会报 `Not a valid CAD file !!`。所以按扩展名分流：

| 扩展名 | 导入函数 |
|---|---|
| `.step .stp .igs .iges .jt .x_t .sldprt .ansa …` | `base.Open` |
| `.nas .bdf .dat` | `base.InputNastran` |
| `.inp .inc` | `base.InputAbaqus` |
| `.key .k .dyna` | `base.InputLSDyna` |
| `.cdb .ans` | `base.InputAnsys` |
| `.pc` / `.rad` / `.pat` / `.stl` | 对应 `base.Input*` |

扩展名有歧义时（例如是 Nastran 的 `.dat`）用 `file_format="nastran"` 显式指定。

## 写操作安全

### 为什么需要幂等

一次调用可能在 **ANSA 已经开始改模型之后**才失败：客户端超时、socket 断开、
MCP 客户端被中断。此时"重试"会把改动**执行两次**——网格划两遍、位移施加两遍、
实体删两遍，大模型上极难发现。

所以每个写工具都带 `operation_id`。服务器在**发送脚本之前**先把操作记到
`.ansa_mcp_ledger.json`，事后再记录结果：

| 记录状态 | 用同一 id 重试时 |
|---|---|
| `success` | 返回存档结果，不再调用 ANSA |
| `pending` | 拒绝，返回 `OUTCOME_UNKNOWN` |
| `unknown` | 拒绝，返回 `OUTCOME_UNKNOWN` |
| `failed` | 允许重跑（已确认没落到模型上） |
| 参数不同 | 拒绝——这个 id 已属于另一个操作 |

记录落盘，MCP 服务重启后仍有效；上限 256 条，满了拒绝新写入而不是驱逐旧记录。

### 三种失败必须分开

| 情况 | 分类 | 行为 |
|---|---|---|
| 连不上 / 发送失败 | `TRANSPORT_FAILED` | 可安全重试，脚本没进 ANSA |
| 已发送、响应超时或断连 | `OUTCOME_UNKNOWN` | **不重试**，先回读模型 |
| 脚本自己抛异常 | `SCRIPT_ERROR` | 可重试，ANSA 明确报告失败 |

重连路径只重连、**不自动启动新 ANSA**——否则脚本会跑在空白库上，看着成功其实与模型无关。

### 证据分层

每次返回末尾有一行：

```
evidence: transport=ok · script=returned · model=GRID +1 · engineering=unverified
```

- `transport` —— 消息有没有送达
- `script` —— 脚本有没有正常返回
- `model` —— 调用前后实体计数实际变化（只读调用不测）
- `engineering` —— 永远 `unverified`

数量变化只能证明"数量变了"，不能证明"改对了"。

### 防误操作

| 保护 | 行为 |
|---|---|
| 覆盖保护 | `save` / `export` 目标已存在时默认拒绝 |
| 整库替换 | `open(merge=false)` 须显式 `confirm=true` |
| 严格布尔 | `confirm=1`、`"true"` 这类伪装值直接拒绝 |
| 外部会话 | `shutdown` 只退出本服务启动的实例 |
| 孤儿清理 | 按 ANSA **真实 PID** 并复核镜像名后才 taskkill |
| 工作区约束 | 设了 `ANSA_MCP_WORKSPACE` 后越界写出直接拒绝 |

## Skill：ansa-midsurface-mesh

把闭合薄壁 CAD 描述变成可求解的壳网格：抽中面、按目标尺寸划网格、把实测壁厚写进 PSHELL。

安装：把 `skills/ansa-midsurface-mesh/` 复制到 agent 的 skill 目录
（WorkBuddy / Claude Code 为 `~/.workbuddy/skills/`）。

用法（在 `ansa_run_python` 里执行）：

```python
# 单件：判定 → 修复 → Skin 抽中面 → property 命名 → 划网格 → 存 .ansa + .nas
PARAMS = {
    "catpart": r"D:/proj/B0029_A0072600089.CATPart",
    "engine": "mesh",                       # "mesh" | "batchmesh"
    "mpar": r"D:/proj/5MM.ansa_mpar",
    "qual": r"D:/proj/5MM.ansa_qual",
    "save": r"D:/out/A0072600089.ansa",
    "nas": r"D:/out/A0072600089.nas",
}
exec(open(r"<skill>/scripts/process_part.py", encoding="utf-8").read(), globals())
RESULT
```

单件流程五步：

1. `base.DetectSolidDescription(estimate_thickness=True)` 一次调用同时给出**钣金判定**和壁厚估计；
   面积占比 <50% 或主导厚度 >4 mm 判为非钣金，直接跳过（返回 `-1.0` 表示估计失败，此时仍尝试抽中面）。
2. `base.Skin` 抽中面；抽不出时依次做 `CheckAndFixGeometry` → `RebuildTopology` 两级修复再试。
3. property 按编号重命名：原始几何 `A0072600089_origin`，中面 `A0072600089_T171`（厚度 ×100 四舍五入）。
4. `mesh.Mesh` 或 Batch Mesh 划网格，由 `.ansa_mpar` + `.ansa_qual` 驱动。
5. 存 `.ansa`（原始几何 + 中面 + 网格）并导出 `.nas`（材料卡 `DEFINED=YES`，否则 MAT1 会被静默丢掉）。

```python
# 批量：一个目录几百件，断点续跑
PARAMS = {
    "folder": r"D:/proj/parts",
    "out_dir": r"D:/out",
    "engine": "mesh",
    "mpar": r"D:/proj/5MM.ansa_mpar",
    "qual": r"D:/proj/5MM.ansa_qual",
    "nas": True,
    "limit": 25,          # 本次最多处理 25 件，0 = 不限
    "resume": True,       # 按 _progress.jsonl 续跑
}
exec(open(r"<skill>/scripts/process_folder.py", encoding="utf-8").read(), globals())
RESULT["summary"]["issues"]      # 必须汇报，不能只报成功数
```

批量跑大目录的做法：每件追加一行到 `_progress.jsonl`；`limit` 分块；需要更快时开 3 个 ANSA 实例，
用 `only=<文件名清单>` 切不相交的子集、各自的 `_progress_w<n>.jsonl`、`write_summary=False`
（并发重写同一个 CSV 会冲突）。

在整车模型上的实测结果（ANSA 24.1.2，540 件目标，含标准件与厚板）：

| 批次 | 零件 | 出网格 | 非钣金 / 抽不出中面 | 壳单元 |
|---|---|---|---|---|
| 主线（Skin） | 540 | 382（70.7%） | 129 标准件 / 9 未解决 | 1 742 602 |
| Casting 二遍救援 | 24 | 20 | 4（需人工或质量等效） | 含在上行 |

标准件 129 件按件号/中文品名判定，不抽中面；9 件未解决里 4 件是泡沫/贴条类厚壁件。

### 两条网格路线

`process_part.py` 是单件完整流水线（钣金判定 → 几何修复 → `Skin` 抽中面 → property 命名 → 划网格 → 保存）；
`mesh_engine.py` 只负责划网格，用 `PARAMS["engine"]` 切换。两条都吃 `.ansa_mpar` + `.ansa_qual`，
都**不负责抽中面**。没有 `.ansa_mpar` / `.ansa_qual` 时用 `rescue_midsurf.py`（`MidSurfAuto` 自带网格器），
但那条路的网格不受项目判据文件控制，只在不要求网格规格时使用。

| engine | 对应 GUI | 特点 |
|---|---|---|
| `"mesh"` | Shell Mesh > Mesh | 特征处理全生效，含会改几何的圆角锐化；`mesh_change_option` 控制 Change 档位 |
| `"batchmesh"` | Batch Mesh Manager | 多件并行、可重复，适合整车批量 |

**不要用网格指标去比较这两个引擎**：同一份中面、同一次会话里，`mesh.Mesh` 第一遍 204 单元 / aspect 2.864，
删掉再划第二遍 218 / 2.345，批处理第三遍 208 / 2.861。差异来自"第几遍"（`change_option = macro perimeters`
允许改写周长分布），不是引擎；要比只能在同一个 Skin 结果的**第一遍**上比。

判据文件确实生效的验证办法：复制一份 mpar 把 `target_element_length` 改成 8，网格边长应随之变化（实测 4.78 → 6.71 mm）。

详见 [skills/ansa-midsurface-mesh/SKILL.md](skills/ansa-midsurface-mesh/SKILL.md) 和
[references/ansa-api-verified.md](skills/ansa-midsurface-mesh/references/ansa-api-verified.md)，
后者记录了**验证过的签名**和**走不通的路**（例如 `BatchGenerator` 不接受尺寸参数）。

## 常用代码片段

```python
# 收集所有壳属性并按厚度分组
deck = base.CurrentDeck()
props = base.CollectEntities(deck, None, "__PROPERTIES__")
{str(p._id): base.GetEntityCardValues(deck, p, ["T"]) for p in props}

# 新建模型（清空）
session.New("blank")

# 给实体改名
ent = base.GetEntity(deck, "PSHELL", 100)
base.SetEntityCardValues(deck, ent, {"Name": "floor_panel"})

# 跨调用保存句柄
ansavars["my_props"] = base.CollectEntities(deck, None, "PSHELL")
```

ANSA 的 Python 绑定**没有 docstring**，签名靠 `ansa_api_search` 查；
实体字段名不要猜，用 `ent.card_fields(deck)` 列出来：

```python
ent = base.CollectEntities(deck, None, "PSHELL")[0]
fields = ent.card_fields(deck)      # ['keyword','PID','MID1','T',...]
base.GetEntityCardValues(deck, ent, fields)
```

## 测试

```bash
python tests/test_ledger.py          # 离线，不启动 ANSA，不占 license
python tests/test_mcp.py             # MCP stdio 契约
python tests/test_server.py batch    # 真实 batch 实例，跑完自动退出
```

见 [tests/README.md](tests/README.md)。

## 已知限制

- **类型名是 deck 卡名**：Nastran deck 里节点是 `GRID`（不是 `NODE`），壳单元是 `SHELL`。
  `CollectEntities(deck, None, "NODE")` 会静默返回 0。
- **`base.GetKeywordFieldLabels` 在本版本返回空列表**，枚举字段用 `ent.card_fields(deck)`。
- **`ansa_screenshot` 需要 gui 模式**：batch 模式没有图形窗口，函数返回 0 但不产生文件。
- **只支持单实例**：一个服务端进程管一个 ANSA 进程。
- **IAP 是同步的**：ANSA 在主线程执行脚本且无法取消，长任务会冻结 GUI，超时后只能等它跑完。
- **不做连接建模**：各零件独立划网格，节点不共享，焊点/tie/rigid 需另外定义。
  这是当前能力边界，下一阶段要做连接能力，方案与坑见
  [docs/交接_连接能力_2026-11-09.md](docs/交接_连接能力_2026-11-09.md)。
- 服务端 stdout 必须保持干净（MCP stdio 通道），ANSA 子进程输出重定向到 `logs/ansa_<port>.log`。

## 实测性能（ANSA 23.0.0 / GT 710 / Win10）

| 操作 | 耗时 |
|---|---|
| 启动 ANSA（gui） | 12.8 s |
| 启动 ANSA（batch） | 4.0 s |
| 导入 64 MB STEP | 46.9 s → 11675 个 CAD 面、109 个 PSHELL |
| BatchMesh 3 个属性 | 16.7 s → 6341 个 SHELL 单元 |
| 截图（1920×1080） | 0.9 ~ 1.2 s |
| 抽中面 + 5mm 网格（17 件总成） | 278 s |

## License

MIT，见 [LICENSE](LICENSE)。
