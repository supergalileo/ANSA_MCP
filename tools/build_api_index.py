"""把 ANSA 自带的 Sphinx RST 文档编译成 `ansa_api_search` 用的索引 JSON。

ANSA 安装目录下的 `docs/extending/python_api/html/_sources/reference/api_ref_*/*.rst.txt`
含 `.. py:function:: Name(signature)` 和完整的参数/return 说明，直接解析即可。

用法：
    python tools/build_api_index.py                        # 自动找本机 ANSA
    python tools/build_api_index.py --ansa-home F:\\ANSA\\ansa_v23.1.1
    python tools/build_api_index.py --out tools/ansa_api_index.json

输出 schema（与 ansa_mcp_server.ansa_api_search 期望的一致）：
    {"ansa_home": ..., "anki_version": ..., "count": N,
     "functions": [{"name","module","signature","description","keywords","examples"}]}
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DEFAULT_CANDIDATES = [
    Path(r"F:\ANSA\ansa_v23.1.1"),
    Path(r"F:\BETA_CAE_Systems\ansa_v25.0.0"),
    Path(r"F:\BETA_CAE_Systems2021\ansa_v21.0.1"),
    Path(r"C:\Program Files\BETA_CAE_Systems\ansa_v23.0.0"),
]

FUNC_RE = re.compile(r"^\.\.\s+py:function::\s+(.+?)\s*$")
MODULE_RE = re.compile(r"^\.\.\s+py:module::\s+(.+?)\s*$")
CLASS_RE = re.compile(r"^\.\.\s+py:class::\s+(.+?)\s*$")
ANY_DIRECTIVE_RE = re.compile(r"^\.\.\s+\w[\w:]*::")

# 描述里这些行是噪音
NOISE_PREFIXES = (":type ", ":rtype:", ":param ", ":returns:", ":remarks:",
                  ":raises", ":ivar", ":cvar", ":vartype")


def find_ansa_home(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not (p / "docs").is_dir():
            raise SystemExit(f"{p} 下没有 docs 目录，确认是 ANSA 安装根目录")
        return p
    for c in DEFAULT_CANDIDATES:
        if (c / "docs/extending/python_api/html/_sources").is_dir():
            return c
    raise SystemExit("没找到带 Sphinx 文档的 ANSA 安装，用 --ansa-home 指定")


def clean_body(lines: list[str]) -> str:
    """去掉 RST 的行首竖线、缩进和参数/返回字段，只留自然语言描述。"""
    out: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            continue
        if s.startswith(NOISE_PREFIXES):
            continue
        s = s.lstrip("|").strip()
        if not s:
            continue
        if s.startswith(".."):
            continue
        out.append(s)
    text = "\n".join(out).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def camel_tokens(name: str) -> list[str]:
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+", name)
    return [p.lower() for p in parts if len(p) > 1]


def keywords_for(name: str, description: str, limit: int = 12) -> list[str]:
    kws = camel_tokens(name)
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", description.lower()):
        if w not in kws:
            kws.append(w)
        if len(kws) >= limit:
            break
    return kws[:limit]


def parse_rst(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    module = path.stem.replace(".rst", "")
    prefix = "ansa."
    stem = path.name.split(".")[0]
    if stem and stem not in ("index", "ansa"):
        prefix = f"ansa.{stem}"

    funcs: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        mmod = MODULE_RE.match(line)
        if mmod:
            module = mmod.group(1)
            i += 1
            continue
        m = FUNC_RE.match(line)
        mcls = CLASS_RE.match(line)
        if mcls:
            # 记录所属 class，用于 module 细化（Class.method 形式在 ANSA 里少见，先跳过）
            i += 1
            continue
        if not m:
            i += 1
            continue

        sig_raw = m.group(1).strip()
        name = re.split(r"[(<]", sig_raw, maxsplit=1)[0].strip()

        # 收集函数体：直到下一个顶层指令（列 0 的 .. py:xxx::）
        body: list[str] = []
        j = i + 1
        while j < n:
            nxt = lines[j]
            if ANY_DIRECTIVE_RE.match(nxt):
                break
            # 章节标题（下划线行）也算结束
            if nxt and set(nxt.strip()) <= set("*=-~^") and len(nxt.strip()) > 3:
                break
            body.append(nxt)
            j += 1

        # 代码片段：以 >>> 或明显 python 调用的行块
        examples = [ln.strip() for ln in body
                    if ln.strip().startswith((">>>", "import ", "from ", "base.",
                                              "mesh.", "morph.", "cad.", "connections.",
                                              "dm.", "script.", "batchmesh."))]
        desc = clean_body(body)
        if len(desc) > 800:
            desc = desc[:800].rsplit(" ", 1)[0] + " …"

        funcs.append({
            "name": name,
            "module": module,
            "signature": f"{module}.{sig_raw}" if module else sig_raw,
            "description": desc,
            "keywords": keywords_for(name, desc),
            "examples": "\n".join(examples[:12]),
        })
        i = j
    return funcs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ansa-home", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    home = find_ansa_home(args.ansa_home)
    src_root = home / "docs/extending/python_api/html/_sources/reference"
    rsts = sorted(src_root.rglob("*.rst.txt"))
    if not rsts:
        raise SystemExit(f"{src_root} 下没有 RST 源")

    all_funcs: list[dict] = []
    per_file: dict[str, int] = {}
    for p in rsts:
        got = parse_rst(p)
        if got:
            all_funcs.extend(got)
            per_file[p.name] = len(got)

    # 去重（同名同模块只留一条）
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for f in all_funcs:
        key = (f["module"], f["name"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)

    out = Path(args.out) if args.out else Path(__file__).with_name("ansa_api_index.json")
    payload = {
        "ansa_home": str(home),
        "count": len(uniq),
        "sources": per_file,
        "functions": uniq,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    print(f"ANSA: {home}")
    print(f"RST files: {len(rsts)}")
    print(f"functions: {len(uniq)} (raw {len(all_funcs)})")
    print(f"index: {out}  ({out.stat().st_size / 1e6:.2f} MB)")
    top = sorted(per_file.items(), key=lambda x: -x[1])[:5]
    print("top modules:", ", ".join(f"{k}={v}" for k, v in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
