# MCP Server —— 让 AI Agent 直接查中国行政区划

> 本仓库附带一个 [MCP](https://modelcontextprotocol.io) 服务，把区划解析能力交给
> Claude Desktop / Claude Code / Cursor 等支持 MCP 的 AI 客户端。
>
> **为什么需要它**：语言模型擅长理解和改写文字，但它分不清「一个行政区划是否真实存在」——
> 那是一次**查找**，不是一次**生成**。模型遇到不确定的地址会顺着语感编一个，而且不会告诉你
> 它在编。本服务提供那次查找，**查不到时诚实返回失败**。

---

## 一、安装与运行

三种方式，按省事程度排。**先看方式一**——不需要克隆仓库、不需要虚拟环境、不需要手动装依赖。

### 方式一：`uvx`（已发布到 PyPI 后）

配置里直接写：

```json
{
  "mcpServers": {
    "quhua": { "command": "uvx", "args": ["quhua-mcp"] }
  }
}
```

需要机器上有 [`uv`](https://docs.astral.sh/uv/)（`pip install uv` 或官方安装脚本，单文件、无依赖）。

### 方式二：`uv run` 直接跑仓库里的脚本（已克隆仓库）

`mcp/mcp_server.py` 顶部带 [PEP 723](https://peps.python.org/pep-0723/) 内联依赖声明，
所以 uv 会**自己把依赖准备好**——不用建虚拟环境，也不用 `pip install`：

```bash
uv run --no-project mcp/mcp_server.py     # 先本地试跑，Ctrl-C 退出
```

配置里：

```json
{
  "mcpServers": {
    "quhua": {
      "command": "uv",
      "args": ["run", "--no-project", "/absolute/path/to/real-china-division/mcp/mcp_server.py"]
    }
  }
}
```

### 方式三：手动（用已有的 Python 环境）

```bash
pip install "mcp>=1.9" pypinyin
```

配置里 `command` 指向**装了依赖的** Python 解释器，`args` 指向 `mcp/mcp_server.py`。

### 依赖说明

- **`mcp`** —— 官方 MCP Python SDK，必需。
- **`pypinyin`** —— 错字纠错依赖它。未安装服务**不会出错**，只是「山冬省 → 山东省」这类错字不会被纠正
  （查不到时仍然诚实拒绝，不会编造，详见 §四）。本项目的 PyPI 包已把它列为硬依赖，
  目的就是让不同用户拿到一致的结果。

### MCP SDK 版本

本服务**同时兼容 MCP Python SDK 1.x 与 2.x**——2.x 把 `FastMCP` 改名为 `MCPServer` 且导入路径也变了，
官方不提供兼容垫片。你不需要关心自己装到的是哪一版。

## 二、配置

⚠️ **各客户端的配置格式并不统一**——多数用 `mcpServers`，但 opencode 是另一套 schema。
下面按客户端给，**路径请换成本机绝对路径**。

### Claude Desktop / Cursor / Trae / WorkBuddy（共用 `mcpServers` 格式）

```json
{
  "mcpServers": {
    "quhua": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/real-china-division/mcp/mcp_server.py"]
    }
  }
}
```

| 客户端 | 配置位置 |
|---|---|
| Claude Desktop | `claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` |
| Trae | 设置 → MCP → 添加 → 手动添加（粘贴上面的 JSON）；或项目级 `.trae/mcp.json` |
| WorkBuddy | `~/.workbuddy/mcp.json`；写入后还需在**连接器管理页**对该服务点「信任」才会生效 |

> ⚠️ **Trae 有额外限制**：`command` 字段**不能含空格**，含空格的路径会导致解析错误。
> 若 Python 装在 `Program Files` 这类目录下，请改用无空格的路径（或把虚拟环境建在无空格处）。

### Claude Code

```bash
claude mcp add quhua -- /absolute/path/to/python /absolute/path/to/mcp/mcp_server.py
```

### opencode

opencode 用**自己的 schema**：顶层键是 `mcp`（不是 `mcpServers`），`command` 是**数组**，
环境变量键名是 `environment`（不是 `env`）。写入 `~/.config/opencode/opencode.jsonc`
（或项目根的 `opencode.json`）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "quhua": {
      "type": "local",
      "command": [
        "/absolute/path/to/python",
        "/absolute/path/to/real-china-division/mcp/mcp_server.py"
      ],
      "enabled": true
    }
  }
}
```

验证：`opencode mcp list` 应显示 `✓ quhua connected`。

### Windows 提示

`command` 写 Python 解释器**完整路径**（正斜杠或转义反斜杠均可），且该解释器须已装依赖。
不想动系统 Python 就先建虚拟环境：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install mcp pypinyin
```

## 三、可用工具

| 工具 | 用途 | 典型问题 |
|---|---|---|
| `resolve_address` | 脏地址、地名、旧称、口语 → 标准区划路径与 12 位码 | "浙江省东阳市横店镇 xx 路"、"襄樊市是哪里" |
| `lookup_code` | 按 6/12 位码查询；**历史码自动回溯到现行区划** | "432221 是哪"（1981 年已废止的码） |
| `list_children` | 列下级区划（省→地→县→乡镇） | "金华市下辖哪些县" |
| `search_changes` | 检索 1980 年以来的变更事件 | "东阳什么时候撤县设市的" |
| `verify_division` | **校验一个区划是否真实存在**——防 AI 编造 | "东北省存在吗" |

另有一个资源 `quhua://dataset-info`，提供数据规模、口径与已知边界的机器可读说明。

### 返回值约定（`status` 字段）

调用方应据此分支，**不要把所有返回都当成"解析成功"**：

| status | 含义 | 期望行为 |
|---|---|---|
| `resolve` | 唯一确定 | 直接使用 `result` |
| `historical` | 输入是已废止的旧地名/旧码 | 使用映射后的现行区划，`note` 给出依据 |
| `ambiguous` | 同名多处存在 | 展示候选集让用户确认，**不要替用户选** |
| `unresolvable` | 无法确定 | **如实告知用户，不要推测补全** |

`confidence` 是**解析路径的可信度档位，不是准确率**，不宜向最终用户展示为"置信度 90%"。

## 四、已知边界

以下事情本服务做不到，被问到时会如实返回失败而不是猜：

1. **不提供坐标，不做逆地理编码**——区划与地理边界是两套体系。
2. **不做门牌级或地址真实性核验**——只能判断地址中的*行政区划部分*是否成立。
3. **不合并双口径**：统计口径的城乡分类代码自 2024-10 起无公开渠道，本数据集不提供也不编造。
4. **1980 年以前为空**。
5. **近音字/多音字不自动纠正**；**泛称不猜**（"开发区"不指向具体某地）；**同名不给唯一答案**。
6. **开发区/新区/园区/兵团**不是民政正式建制，标注为"特殊口径"而非硬归。
7. **时间机器在本发行版不可用**（不随包分发 GB/T 2260 逐年快照）——历史归属问题请用 `search_changes`。
8. **未覆盖 7 个特殊县域**的村级数据（金门、三沙西沙/南沙、西藏岗巴/噶尔、云南大姚、新疆和安）。

### 关于 `pypinyin` 的诚实说明

错字纠错依赖读音验证。以「东北省」为例——它与真实存在的「河北省」只差一个字，
**只有读音能区分二者**（dōngběi ≠ héběi）。

- **装了 `pypinyin`**：按读音判断，"山冬省 → 山东省"能纠正，"东北省"被拒绝。
- **没装**：服务**放弃这类模糊匹配**（宁可漏纠，不可误报），"东北省"同样被拒绝，
  但"山冬省"也**不会**被纠正。

两种情况都**不会**把不存在的区划说成存在。区别只是纠错能力的多少。

## 五、许可

与本仓库一致：代码 MIT，数据 CC BY 4.0。

---

## 七、分发与登记（让别人发现这个服务）

上传 PyPI 只是货架，不等于有人来。MCP 生态的发现路径按优先级：

| 顺序 | 渠道 | 动作 | 状态 |
|---|---|---|---|
| 1 | **官方 MCP Registry**（registry.modelcontextprotocol.io） | `mcp-publisher login github`（浏览器授权）→ `mcp-publisher publish`；`server.json` 已备在 `mcp/`，命名空间 `io.github.nbsetsail/quhua-mcp` | ☐ |
| 2 | **punkpeye/awesome-mcp-servers**（83k★，流量最高的发现面） | 向该仓库提 PR，条目文案见 `mcp/MCP.md` §七.1 | ☐ |
| 3 | **Glama**（自动爬 GitHub，但需认领） | glama.ai/mcp/servers → Add Server → 提交仓库 URL | ☐ |
| 4 | **MCP.so** | 官网 Submit → GitHub issue（5 分钟） | ☐ |
| 5 | **Smithery** | smithery.ai → Publish MCP → 连接本仓库（stdio 服务列为 self-hosted） | ☐ |
| 6 | **GitHub 仓库本身** | topics 补 `mcp` / `model-context-protocol` / `china`；README 已含安装段 | ☐ |

### 七.1 awesome-mcp-servers 条目文案（提 PR 直接粘）

```markdown
- [quhua-mcp](https://pypi.org/project/quhua-mcp/) - 中国行政区划权威事实源：地址解析清洗、
  编码直查与历史码回溯（1981-2026）、下级区划、变更事件检索、区划存在性校验。
  查不到时诚实返回 `unresolvable` 而不是编造，同名歧义返回候选集。
  纯本地 stdio，`uvx quhua-mcp` 一行接入 Claude Desktop / Cursor / Claude Code。
```

### 七.2 纪律提醒

MCP 的定位是**试金石 + 可验证资产**，不是获客渠道（止损线：3 个月真实使用者 ≈ 0 即封存宣传）。
所以分发只做**登记型动作**（合计 ~1 小时），不投内容营销；上面 1、2 两项做完，其余随缘。
