"""列出本机所有 ANSA 安装，并切换 `~/.workbuddy/mcp.json` 里 ansa 服务的 ANSA_HOME。

用法：
    python tools/switch_ansa.py            # 只列出，不修改
    python tools/switch_ansa.py 23.1.1     # 切到 23.1.1（按版本号模糊匹配）

切换只改 mcp.json 的 ansa.env.ANSA_HOME，其他配置一律保留。
改完需要重开 MCP 会话才生效。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MCP_JSON = Path.home() / ".workbuddy" / "mcp.json"

# 常见安装根目录，按需增补
ROOTS = [
    Path(r"F:\ANSA"),
    Path(r"F:\BETA_CAE_Systems"),
    Path(r"F:\BETA_CAE_Systems2021"),
    Path(r"C:\Program Files\BETA_CAE_Systems"),
    Path(r"D:\BETA_CAE_Systems"),
    Path(r"D:\Program Files\BETA_CAE_Systems"),
]


def find_installs() -> list[tuple[str, Path]]:
    """返回 [(版本号, 安装目录), ...]，只收含 ansa64.bat 的目录。"""
    out: list[tuple[str, Path]] = []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            m = re.search(r"ansa_v?(\d+\.\d+(?:\.\d+)?)", d.name, re.I)
            if m and (d / "ansa64.bat").is_file():
                out.append((m.group(1), d))
    # 版本号降序
    out.sort(key=lambda x: [int(p) for p in x[0].split(".")], reverse=True)
    return out


def current_home() -> str:
    if not MCP_JSON.is_file():
        return ""
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    return data.get("mcpServers", {}).get("ansa", {}).get("env", {}).get("ANSA_HOME", "")


def set_home(target: Path) -> None:
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    env = data.setdefault("mcpServers", {}).setdefault("ansa", {}).setdefault("env", {})
    env["ANSA_HOME"] = str(target)
    MCP_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    installs = find_installs()
    if not installs:
        print("没找到任何 ANSA 安装，检查 ROOTS 列表。")
        return 1

    cur = current_home()
    print(f"当前 ANSA_HOME: {cur or '(未设置)'}\n")
    print(f"{'#':<3}{'版本':<12}{'路径'}")
    for i, (ver, path) in enumerate(installs, 1):
        mark = "  <- 当前" if str(path).lower() == cur.lower() else ""
        print(f"{i:<3}{ver:<12}{path}{mark}")

    if len(sys.argv) < 2:
        print("\n只列出。要切换：python tools/switch_ansa.py <版本号或序号>")
        return 0

    want = sys.argv[1].strip()

    # 支持按序号
    if want.isdigit() and 1 <= int(want) <= len(installs):
        ver, target = installs[int(want) - 1]
    else:
        hits = [(v, p) for v, p in installs if want in v]
        if not hits:
            print(f"\n没有版本匹配 {want!r}")
            return 1
        if len(hits) > 1:
            print(f"\n{want!r} 匹配到多个：{[v for v, _ in hits]}，写得更具体些")
            return 1
        ver, target = hits[0]

    set_home(target)
    print(f"\n已切到 {ver}: {target}")
    print("重开 MCP 会话后生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
