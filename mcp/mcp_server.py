#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区划数据 · MCP Server
=====================

把中国行政区划解析能力暴露给 AI Agent（Claude Desktop / Cursor / Claude Code 等）。

设计立场：**AI 需要的是"权威事实源"，不是又一个生成地址的模型。**
语言模型擅长理解与改写文字，但它无法知道某个区划是否真实存在——那是一次*查找*，
不是一次*生成*。本服务提供那次查找，并在查不到时**诚实返回失败，而不是编造**。

依赖：`pip install mcp`（仅此一项；解析逻辑与数据复用本仓库的 engine.py）
运行：`python mcp_server.py`（stdio 传输，由 MCP 客户端拉起）

纪律：**本文件不得向 stdout 输出任何内容**——stdio 传输用 stdout 传 JSON-RPC，
任何多余 print 都会破坏协议。调试信息一律走 stderr。
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.9", "pypinyin>=0.50"]
# ///
# ↑ PEP 723 内联依赖声明。装了 uv 的人可以**不建虚拟环境、不 pip install** 直接跑：
#     uv run mcp_server.py
#   uv 会按上面的声明自动准备好依赖。这是本项目推荐的零配置试用方式。
import sys
from pathlib import Path

# ── 仓库布局引导 ────────────────────────────────────────────────────────────
# 本文件在 mcp/ 子目录里，而 engine.py / aliases.json / data/ 在仓库根。
# 克隆仓库直接 `python mcp/mcp_server.py` 时 sys.path[0] 是 mcp/，必须把根目录
# 插进来才能 `import engine`。装成 wheel 后 engine 与数据在包内同级，这行是幂等的。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 协议护栏 ────────────────────────────────────────────────────────────────
# engine 在数据加载失败时会 print 警告；import 期间先把 stdout 引到 stderr，
# 避免任何意外输出污染 JSON-RPC 通道。import 完成后恢复（MCP 随后要正常用 stdout）。
_stdout_guard = sys.stdout
sys.stdout = sys.stderr
try:
    import engine
    # MCP Python SDK 2.x 把 `FastMCP` 改名为 `MCPServer`、导入路径也随之变化，且官方**不提供兼容垫片**。
    # 1.x 仍在维护线、2.x 已是 PyPI 最新版，两边都有用户在跑，所以这里**同时支持**——
    # 不让用户「装到哪一版靠运气」。（2.2.0 上 `from mcp.server.fastmcp import FastMCP` 会直接抛
    # ModuleNotFoundError，2026-09-18 实测。）
    try:
        from mcp.server.mcpserver import MCPServer as _ServerBase
        _MCP_V2 = True
    except ImportError:
        from mcp.server.fastmcp import FastMCP as _ServerBase
        _MCP_V2 = False
finally:
    sys.stdout = _stdout_guard

VERSION = "0.3.0"

INSTRUCTIONS = """\
中国行政区划解析工具。当用户的问题涉及中国地址、行政区划名称、区划代码、
"某地属于哪里"、"某地以前叫什么/属于谁"、"某市下辖哪些县"时，调用本服务而不是靠记忆回答——
行政区划每年都在变（撤县设区、更名、改码），模型的记忆会过期，而本数据截至 2025-12-31。

重要约束（请向用户如实传达）：
- 解析不到时本服务返回 status=unresolvable，**此时不要自行推测补全**，如实告知用户无法确定。
- 同名区划返回 status=ambiguous 与候选集，**不要替用户挑一个**，请让他确认。
- 开发区/新区/园区不是正式行政区划，会被标注为"特殊口径"，不要当成正式建制。
- 置信度 confidence 是解析路径的可信度档位，不是准确率。
"""

# ⚠️ 构造参数一律用**关键字**：v2 的位置参数顺序改成了 name, title, description, instructions…，
# 若把 instructions 放在第二位，在 v2 上**不报错但会静默错位**（被当成 title，instructions 不再发送）。
if _MCP_V2:
    # 2.x 起 version 是正经的构造参数
    mcp = _ServerBase("quhua", instructions=INSTRUCTIONS, version=VERSION)
else:
    mcp = _ServerBase("quhua", instructions=INSTRUCTIONS)
    # 1.x 的构造函数**不接受** version：该参数会落进 **settings 被静默丢弃，
    # 导致服务自报的是 MCP SDK 的版本号（如 1.9.4）而非本服务的版本——
    # 使用方无从判断自己连的是哪一版**数据**。底层 lowlevel Server 有该字段、只是没透出，这里补设。
    mcp._mcp_server.version = VERSION


# ── 工具 ────────────────────────────────────────────────────────────────────
@mcp.tool()
def resolve_address(address: str) -> dict:
    """把中文地址、地名或口语称呼解析成标准行政区划路径与 12 位区划码。

    适用场景：脏地址标准化、判断"某镇属于哪个县"、旧地名归位、口语地标识别。
    输入可以是完整地址（"浙江省东阳市横店镇 XX 路 8 号"）、
    区划名称（"东阳市"）、旧地名（"襄樊市"）、或简称/口语（"中关村"）。

    返回的 status 表示结果性质，务必据此处理：
      - resolve      唯一确定，result 即标准路径
      - historical   输入是已废止的旧地名或旧码，已映射到现行区划，note 给出依据
      - ambiguous    同名多处存在，result 为候选集，请让用户确认而非自行选定
      - unresolvable 无法确定，此时**不要编造**，如实告知用户
    confidence 为解析路径可信度档位（非准确率）；codes 为各级 12 位码。
    """
    if not address or not address.strip():
        return {"status": "error", "note": "地址不能为空"}
    return engine.resolve_any(address.strip())


@mcp.tool()
def lookup_code(code: str) -> dict:
    """按行政区划代码查询对应的区划路径。

    支持 6 位（省/市/县）与 12 位（含乡镇街道）码。
    若输入是 1980 年以来已废止的历史码，会回溯映射到现行区划并说明变更依据——
    这是处理老系统里旧编码的入口。
    """
    if not code or not code.strip():
        return {"status": "error", "note": "代码不能为空"}
    return engine.resolve_by_code(code.strip())


@mcp.tool()
def list_children(division: str) -> dict:
    """列出某个行政区划的下级区划。

    division 可以是名称或代码（如 "浙江省" / "330000" / "东阳市"）。
    省级返回地级、地级返回县级、县级返回乡镇街道；乡镇级没有下级。

    典型问题："金华市下辖哪些县"、"这个县有哪些乡镇街道"、"江苏省有多少个地级市"。
    """
    q = (division or "").strip()
    if not q:
        return {"status": "error", "note": "division 不能为空"}

    path = None
    if q.isdigit() and len(q) in (6, 12):
        q12 = q.ljust(12, "0")
    else:
        r = engine.resolve_any(q)
        if r.get("status") == "ambiguous":
            return {"status": "ambiguous", "input": q, "candidates": r.get("result"),
                    "note": "同名多处存在，请让用户确认具体是哪一个"}
        if r.get("status") in ("unresolvable", "error"):
            return {"status": "unresolvable", "input": q,
                    "note": r.get("note") or "无法识别该区划，请勿推测"}
        path = r.get("result")
        codes = r.get("codes") or {}
        raw = codes.get("township") or codes.get("county") or codes.get("city") or codes.get("province")
        if not raw:
            return {"status": "unresolvable", "input": q, "note": "解析结果缺少区划码"}
        q12 = str(raw)

    # 12 位码结构：省·地·县 各占 2/2/2 位，后 6 位为乡镇标识；全 0 段表示该级未细分
    if q12[2:] == "0" * 10:
        for pname, pv in engine.DIV.items():
            if pv.get("code") == q12:
                cities = [{"name": c, "code": pv["cities"][c].get("code")}
                          for c in pv["cities"] if c]
                return {"status": "ok", "parent": pname, "level": "地级",
                        "count": len(cities), "children": cities}
        return {"status": "not_found", "parent": q, "note": "未找到该省级区划"}

    if q12[4:] == "0" * 8:
        for pname, pv in engine.DIV.items():
            for cname, cv in pv["cities"].items():
                if cv.get("code") == q12:
                    counties = [{"name": n, "code": c} for n, c in cv["counties"].items()]
                    return {"status": "ok", "parent": cname or pname, "level": "县级",
                            "count": len(counties), "children": counties}
        return {"status": "not_found", "parent": path or q, "note": "未找到该地级区划"}

    if q12[6:] == "0" * 6:
        towns = engine.COUNTY_TOWNSHIPS.get(q12)
        if towns is None:
            return {"status": "no_data", "parent": path or q, "level": "乡镇级",
                    "count": 0, "children": [],
                    "note": "该县无乡镇街道数据（少量特殊县域未覆盖）"}
        return {"status": "ok", "parent": path or q, "level": "乡镇级",
                "count": len(towns), "children": [{"name": n, "code": c} for n, c in towns.items()]}

    return {"status": "leaf", "parent": path or q,
            "note": "该层级为最末级（乡镇街道），行政区划序列中不再细分"}


@mcp.tool()
def search_changes(q: str = "", year: int = 0, code: str = "",
                   year_start: int = 0, year_end: int = 0, limit: int = 20) -> dict:
    """检索中国行政区划的变更事件（1981 年至今）。

    用来回答："某地是什么时候改的名/撤的县/改的码"、"某年有哪些区划变更"、
    "这个旧代码是哪一次调整造成的"。事件含变更前后名称与代码、完整路径，部分附民政部批复文号。

    参数：q 关键词（区划名）· year 单年 · code 区划码 ·
    year_start/year_end 年份区间 · limit 返回条数上限。
    """
    if not any((q, year, code, year_start, year_end)):
        return {"status": "error", "note": "q / year / code / year_start / year_end 至少提供一个"}
    return engine.query_events(year=year or None, q=q or None, code=code or None,
                               limit=max(1, min(limit, 100)),
                               year_start=year_start or None, year_end=year_end or None)


@mcp.tool()
def verify_division(name_or_code: str) -> dict:
    """校验一个行政区划名称或代码是否真实存在——防止 AI 编造不存在的区划。

    当你需要确认某个区划（尤其是生僻的、记忆中没有把握的）是否真存在时调用本工具。
    例如"东北省"、"浦东新区是正式区划吗"、"430621 是哪里"。

    返回 exists 表示该区划是否成立：
      - exists=true   matched 给出标准路径与级别；若同名多处会附 candidates
      - exists=false  **请不要据此编造替代答案**，如实告知用户该区划不存在或无法确认
    """
    q = (name_or_code or "").strip()
    if not q:
        return {"status": "error", "note": "参数不能为空"}

    if q.isdigit() and len(q) in (6, 12):
        r = engine.resolve_by_code(q)
    else:
        r = engine.resolve_any(q)

    st = r.get("status")
    if st == "ambiguous":
        cands = r.get("result") or []
        return {"input": q, "exists": True, "same_name_count": len(cands),
                "matched": None, "candidates": cands,
                "note": "该名称对应多个区划，请让用户确认具体是哪一个"}
    if st in ("unresolvable", "error"):
        return {"input": q, "exists": False, "matched": None,
                "note": r.get("note") or "未匹配到任何行政区划——如实告知用户，不要推测"}
    if st in ("resolve", "historical"):
        codes = r.get("codes") or {}
        level = None
        for key, label in (("township", "乡镇级"), ("county", "县级"),
                           ("city", "地级"), ("province", "省级")):
            if isinstance(codes, dict) and codes.get(key):
                level = label
                break
        return {"input": q, "exists": True,
                "matched": r.get("result"), "level": level,
                "confidence": r.get("confidence"),
                "was_historical": st == "historical",
                "note": r.get("note")}
    return {"input": q, "exists": False, "matched": None, "note": r.get("note")}


# ── 资源：数据口径与已知边界（让 Agent 知道我们不知道什么）────────────────────
@mcp.resource("quhua://dataset-info")
def dataset_info() -> str:
    """本数据集的口径、规模与已知边界。回答用户前若涉及数据能力范围，应参考本资源。"""
    return f"""\
# 区划数据 · 口径与边界

**数据版本：{VERSION}**

## 规模（截至 2025-12-31 民政口径）
- 省级 34 / 地级 333 / 县级 2,847（合计 3,214 个区划节点）
- 乡镇街道 38,749 · 村级（行政村与社区）604,626
- 变更事件 5,276 条（1981-2026），其中 183 条附民政部年度变更页链接
- 别名/旧名映射 1,214 条

## 口径
- 编码依据 GB/T 2260 及主管部门公布的行政区划变更，编码为 12 位民政口径建制码。
- 村级实体用民政地名口径的 **20 位地名标准码**（前 12 位对应所属县级区划），
  与上级四级的 12 位建制码是两套体系。
- 数据随版本更新再生，不做静默替换。

## 已知边界（这些事本服务做不到，被问到请如实说明）
1. **不提供坐标，也不做逆地理编码**（经纬度 → 区划）。行政区划与地理边界是两套体系。
2. **不做门牌级或地址真实性核验**——只能判定"地址中的行政区划部分"是否成立，
   无法确认某街道某号是否存在、是否可投递。
3. **不合并双口径**：统计口径的村级「城乡分类代码」自 2024-10 起无现行公开渠道，
   本数据集不提供也不编造。
4. **1980 年以前为空**。历史沿革数据（更早的政区变迁）不在本数据集范围内。
5. **近音字/多音字不自动纠正**；**泛称不猜**（如"开发区"不指向具体某地）；
   **同名不给唯一答案**，一律返回候选集。
6. **开发区/新区/园区/兵团**在民政口径中不是正式建制，会被标注为"特殊口径"而非硬归入某地。
7. **未覆盖**：金门、三沙市西沙区/南沙区、西藏岗巴县/噶尔县、云南大姚县、新疆和安县等
   特殊县域暂无村级数据（乡镇级亦可能缺失）。
8. 本发行版**不随包分发 GB/T 2260 逐年快照**，因此**时间机器（回放任意年份区划状态）
   在本版本不可用**；历史归属问题请改用 search_changes 查询变更事件。
9. **早期噪声**：1984 年前全国代码多次重编，事件库在该时期存在代码串扰，
   重要结论请以民政批复原文为准。

## 许可
代码 MIT，数据 CC BY 4.0。可商用，需保留署名。
"""


def main() -> None:
    """入口点（stdio 传输）。

    单独抽成函数是为了让打包分发可用：`[project.scripts]` 与 `uvx <包名>` 都需要一个
    可调用的入口，而不是只在 `__main__` 下执行。
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
