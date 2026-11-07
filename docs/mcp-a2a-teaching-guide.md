# MCP & A2A 协议集成 —— 课堂演示手册

面向老师/助教的现场演示脚本,时长约 15 分钟,覆盖两个独立但彼此对称的协议演示:**A2A**(本项目作为客户端,调用一个独立的 Excel Narrator Agent)和 **MCP**(本项目作为服务端,把自己的检索/问答能力暴露给任意 MCP 客户端)。配套阅读:项目根目录 `README.md` 中的『MCP & A2A Protocol Integration』章节(架构图、端口表、原语表都在那里)。

## 教学目标

三层架构叙事,每层一句话:

- **LangGraph**(进程内编排):在同一个 Python 进程里,用状态图串联 guardrail → retrieval → intent → answer 四个 Agent 节点,这是项目原有的问答工作流。
- **MCP**(Model Context Protocol,纵向 —— agent 连接 tools/resources):把"我们系统的能力"包装成 tool / resource / prompt,暴露给任何愿意说 MCP 协议的客户端(Claude Desktop、MCP Inspector、自定义客户端),对方零自定义集成代码即可使用。
- **A2A**(Agent2Agent,横向 —— agent 连接 agent):把"一整个任务"(把 Excel 转成可检索文本)委托给另一个独立进程里的自治 Agent,通过标准协议做能力发现、消息传递、任务状态跟踪。

对称性是这堂课真正想讲的东西:**MCP 展示"别人怎么用我们",A2A 展示"我们怎么用别人"。**

## 课前准备 Checklist

```bash
# 1. Python 环境:激活装好依赖的环境(例如 README 里的 conda 环境 rag),后面的 python / mcp 命令都假定已激活
conda activate rag

# 2. .env 已经配置好全部 Azure 凭证(Document Intelligence / AI Foundry
#    embedding+GPT4 / AI Search / Application Insights),以及
#    A2A_EXCEL_AGENT_URL、EXCEL_AGENT_USE_LLM 两个 A2A 相关变量
#    (课堂上不要展示 .env 的真实内容,这里只做存在性检查)
grep -c "^AZURE_" .env

# 3. 跑一次 index status,确认配置能连上 Azure、索引可访问
python main.py index status
```

预期输出大致如下(具体数值因你的环境而异):
```
Index Name: ai-earnings-desk
Exists: True
Document Count: <N>
Vector Search: True
Semantic Search: False
Fields: id, content, content_vector, source_file, chunk_index, chunk_size, company, ticker, doc_kind, file_type, fiscal_period, calendar_quarter, metadata
```

> **重要提示**:如果你之前已经在这台机器上完整跑过一次 Demo 1(即 `ai_earnings_comparison.xlsx` 已经被处理过),索引里会**已经包含**这份对比工作簿的内容(可以用 `python main.py retrieve "highest revenue CY2026 Q2 ranking"` 快速确认,看 `Source:` 是否出现 `ai_earnings_comparison.xlsx`)。这种情况下,Demo 1 开场"先问一个答不出的问题"这一步**不会**重现失败——LLM 会直接给出正确答案。两种应对方式:
> 1. 顺势讲解:"索引是持久化的,这就是为什么第二次问同样的问题不需要重新处理文件",把它变成教学素材;或者
> 2. 课前重建索引,获得完整的"先失败、后成功"对比效果:
>    ```bash
>    python main.py index recreate   # 会提示 (yes/no) 确认;这会清空所有已索引内容
>    python main.py ingest --tier 1 --types pdf,html,docx  # 重新索引基线语料(Tier 1 的 PDF/HTML/DOCX),先不要处理两个 XLSX,把对比工作簿留到课堂演示
>    ```

## Demo 1: A2A —— Excel Narrator Agent

### 步骤 1:先问一个只有 Excel 数据才能完整回答的问题

```bash
python main.py query "Which company had the highest revenue in calendar Q2 2026?"
```

在"干净"的环境里(只索引了 Tier 1 的 PDF/HTML/DOCX:各公司各自的新闻稿、CFO 评论和电话会议记录),没有任何一份文档同时比较 8 家公司,而且各家财年口径不同(例如 NVIDIA 的 FY2027 Q2 才对应日历 2026 年第二季度):预期答案只能给出部分公司的数字或者含糊其辞;若开启了 guardrail,也可能被判为不相关。(具体提示文字以实际运行为准,讲课重点是"答不全/答不准",不需要逐字稿。若你的索引里已经有对比工作簿,见上方课前准备的提示。)

### 步骤 2:在 Terminal 1 启动 Excel Narrator Agent

```bash
# Terminal 1,项目根目录下
python -m services.excel_agent
```

预期输出(已实测校验):
```
Starting Excel Narrator Agent on http://127.0.0.1:9999
Agent card: http://127.0.0.1:9999/.well-known/agent-card.json
main app: python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
```

> 如果参考项目 multiagent-rag 的 Agent 还在 9999 端口上运行,本项目的 Agent 会因端口占用而启动失败,而 `main.py` 会连到参考项目的那个 Agent 上。先关掉它,或者见文末"端口冲突"。

保持这个终端开着——这就是一个独立运行的 Agent 进程,和主项目不是同一个 Python 解释器,甚至可以跑在另一台机器上。

### 步骤 3:查看 AgentCard(能力发现)

```bash
curl -s http://127.0.0.1:9999/.well-known/agent-card.json | python3 -m json.tool
```

预期输出(已实测校验,字段为 protobuf JSON 的 camelCase 命名):
```json
{
    "name": "Excel Narrator Agent",
    "description": "Standalone A2A agent that converts spreadsheets (.xlsx, .csv) into descriptive natural-language passages for downstream RAG indexing. The main RAG app never imports this agent's code -- it discovers this AgentCard over HTTP and delegates conversion via the A2A protocol.",
    "supportedInterfaces": [
        {
            "url": "http://127.0.0.1:9999",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0"
        }
    ],
    "version": "0.1.0",
    "capabilities": {
        "streaming": true
    },
    "defaultInputModes": [
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv"
    ],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "excel_to_narrative",
            "name": "Excel to Narrative",
            "description": "Converts an uploaded Excel workbook (.xlsx) or CSV file into descriptive natural-language passages -- one context paragraph per sheet plus one sentence per data row -- suitable for embedding and retrieval in a RAG pipeline.",
            "tags": ["a2a", "rag", "excel"],
            "examples": [
                "Narrate ai_earnings_comparison.xlsx for indexing",
                "Convert a quarterly results CSV into RAG-ready text"
            ],
            "inputModes": [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/csv"
            ],
            "outputModes": ["text/plain"]
        }
    ]
}
```

**讲解点(AgentCard 能力发现)**:主项目里的 `A2AExcelClient` 从来没有 `import` 过 `services.excel_agent` 里的任何一行代码。它只是在运行时对这个 well-known 路径发一个 HTTP GET,拿到这份 JSON,从中读出 `name` / `version` / `skills` / `capabilities.streaming`,就知道了这个 Agent 是谁、能干什么、怎么调用它。这就是 A2A 的能力发现机制——契约是这份 JSON,不是代码。

### 步骤 4:处理对比工作簿 ai_earnings_comparison.xlsx,现场解说流式进度

```bash
# Terminal 2,项目根目录下
python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
```

预期日志(为了课堂可读性,下面省略了每行前面的 `<时间戳> - <logger名> - INFO -` 前缀,只保留消息本身;实际终端输出会带完整前缀,格式是 `2026-08-03 23:02:55,712 - __main__ - INFO - ...`):

```
[1/5] Extracting via A2A Excel Narrator Agent...
Discovered agent 'Excel Narrator Agent' v0.1.0 with skill(s): excel_to_narrative
[Excel Narrator Agent] TASK_STATE_WORKING: Reading ai_earnings_comparison.xlsx...
[Excel Narrator Agent] TASK_STATE_WORKING: Narrating sheet 1/13: 'Quarterly results'...
[Excel Narrator Agent] TASK_STATE_WORKING: Narrating sheet 2/13: 'Revenue'...
...
[Excel Narrator Agent] TASK_STATE_WORKING: Narrating sheet 11/13: 'Rankings'...
[Excel Narrator Agent] TASK_STATE_WORKING: Narrating sheet 12/13: 'Companies'...
[Excel Narrator Agent] TASK_STATE_WORKING: Narrating sheet 13/13: 'Definitions'...
[Excel Narrator Agent] TASK_STATE_COMPLETED: Narrated 13 sheet(s) from ai_earnings_comparison.xlsx.
Sheet metadata from agent data part: 13 sheet(s): Quarterly results, Revenue, Revenue growth YoY, ...
✓ Extracted <N> characters from 13 sheets

[2/5] Chunking text...
✓ Created <N> chunks
  - Avg size: ... chars
  - Min/Max: .../...

[3/5] Generating embeddings...
✓ Generated embeddings for <N> chunks

[4/5] Setting up search index...
✓ Using existing index

[5/5] Indexing documents...
✓ Indexed <N> chunks
```

> 具体的字符数/分片数每次运行可能略有不同——默认 `EXCEL_AGENT_USE_LLM=true`,每个 sheet 会多出一段 LLM 生成的摘要,遣词造句不完全可复现(关掉 LLM 时实测为 251,433 个字符、309 个分片,其中 135 个分片是列视图;再设 `EXCEL_AGENT_COLUMN_VIEWS=false` 关掉列视图,则为 141,131 个字符、174 个分片)。但工作簿里逐行的确定性模板句子(不依赖 LLM)保证了关键事实永远不变,这也是为什么下面步骤 5 的答案要点稳定可复现:工作簿由 `datasets/ai-earnings-desk/scripts/build_comparison.py` 从 SEC XBRL 数据确定性地生成,Rankings 表里 "Highest revenue, CY2026 Q2" 一行永远是 Alphabet 119,796(百万美元)、第二名 NVIDIA 96,221。

现场讲解重点(结合 Terminal 1 里 Agent 侧同时打印的日志一起看):

- **Task 生命周期**:每一条 `TASK_STATE_WORKING` 都是 Agent 完成一个真实子步骤(读文件 → 逐个 sheet 叙述)之后主动推送的状态更新,`main.py` 是通过一条持续打开的流(**streaming**)实时收到这些更新的,不是轮询出来的。
- **Artifact**:全部 sheet 叙述完成后,Agent 把最终结果打包成一个 **artifact** 返回给客户端——叙述文本加上结构化的 sheet 元数据(一个 artifact 里的 text part + data part),随后状态变为 `TASK_STATE_COMPLETED`。
- **raw part 文件传输**:反过来看,课件开始时客户端把 `ai_earnings_comparison.xlsx` 发给 Agent 时,传输的是一个 **raw part**(原始字节 + media_type + 文件名,通过 `a2a.helpers.new_raw_message` 构造),不是把内容转成文本再传——这样二进制格式(.xlsx 本身就是一个 zip 包)才能完整无损地过去。
- **零代码耦合**:`A2AExcelClient` 拿到的是和 `DocumentExtractor` 同款结构的 dict(`text` / `page_count` / `metadata`),后面的 `TextChunker → Embedder → AzureSearchIndexer` 完全不知道、也不需要知道这份文本来自 PDF 还是 Excel Agent。

### 步骤 5:再问一次同样的问题

```bash
python main.py query "Which company had the highest revenue in calendar Q2 2026?"
```

预期答案要点(数字已对照工作簿核实;措辞以实际运行为准):
```
Alphabet: $119,796 million in calendar Q2 2026; runner-up NVIDIA: $96,221 million
sources: ai_earnings_comparison.xlsx, ...
```

现在系统答对了——因为工作簿的叙述文本已经进入了和 PDF/HTML/DOCX 完全相同的 chunk → embed → index 流水线。更多带核实答案的问题(包括同时引用工作簿和 PDF 的跨来源问题)见 `datasets/ai-earnings-desk/INDEXING_PLAN.md` 第 6 节。

## Demo 2: MCP —— Multi-Agent RAG Server

用 MCP Inspector 交互式探索这个 RAG 系统被暴露出来的 MCP server:

```bash
mcp dev src/mcp_server/server.py
```

这会在浏览器里打开 MCP Inspector,直接连接到 `src/mcp_server/server.py` 里定义的 `mcp` 实例,不需要你自己另起一个 server 进程。

### 对比 search_documents 与 ask_rag

在 Inspector 的 Tools 标签页里,依次调用:

- `search_documents(query="Which company had the highest revenue in calendar Q2 2026?", top_k=3)` —— 只做一次 Azure AI Search 的混合检索(向量 + 关键字),**不花一分钱 LLM 调用**,直接返回原始 chunk 列表(`id` / `source_file` / `score` / `content` ...)。
- `ask_rag(question="Which company had the highest revenue in calendar Q2 2026?")` —— 跑完整的 4-agent LangGraph 流水线(guardrail → retrieval → intent → answer),**真正调用 LLM**,返回综合生成的答案,外加 guardrail/intent 的决策过程。

**讲解点**:同一份底层检索能力,一个"只给你原料"(`search_documents`),一个"给你成品"(`ask_rag`)——让学生直观理解"检索"和"生成"在成本、延迟、可控性上的差别,也呼应 `search_documents` 文档字符串里的说明。

### 读取 rag://index/status resource

在 Inspector 的 Resources 标签页里读取 `rag://index/status`,会返回当前索引的名字、文档数量、是否启用向量/语义搜索、字段列表。**讲解点**:这是一个只读的 **resource**,给客户端加载"当前系统状态"用的上下文,不是一个需要 LLM 决定"要不要调用"的 **tool**——它没有参数,也没有副作用,这正是 resource 和 tool 的语义边界。

### cited_answer prompt

在 Inspector 的 Prompts 标签页里用 `cited_answer(question=...)` 生成一段可复用的提示模板,它会指示调用它的 LLM:先调用 `search_documents` 再回答、只用检索到的内容作答、每个论点都要标注来源文件、检索不到就直说找不到而不是瞎编。**讲解点**:这就是 **prompt** 这个原语的典型用法——不是一次性问答,而是一个可以被任何客户端复用的、参数化的提示模板。

### (可选)接入 Claude Desktop

在 `claude_desktop_config.json` 里加入(把路径换成你自己机器上的绝对路径):

```json
{
  "mcpServers": {
    "ma-rag": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "src.mcp_server", "--transport", "stdio"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/ma-rag"
      }
    }
  }
}
```

重启 Claude Desktop 后,就能在对话里直接看到并调用这三个 MCP 原语了。**讲解点**:三原语(tool / resource / prompt)的语义边界,以及"协议标准化"带来的复用性——同一份 `src/mcp_server/server.py` 代码,不用改一行,既能被 Inspector 用,也能被 Claude Desktop 用,以后接入别的 MCP 客户端也是一样,不需要为每个 client 写一份定制集成代码。

## 常见问题与故障排查

### Excel Narrator Agent 没启动就处理文件

```bash
python main.py process datasets/ai-earnings-desk/comparison/ai_earnings_comparison.xlsx
```

若 Terminal 1 里的 Agent 没有启动,会看到:
```
ERROR - Error processing document: Excel Narrator Agent not reachable at http://127.0.0.1:9999. Start it first: python -m services.excel_agent
```
进程以退出码 1 结束。按提示先在另一个终端跑 `python -m services.excel_agent`,确认它打印出 `Starting Excel Narrator Agent on http://127.0.0.1:9999` 之后再重试。

> 备注(可作课堂小讲点):这条友好报错来自 `src/data_pipeline/a2a_excel_client.py`,它同时捕获 `httpx.HTTPError` 和 a2a-sdk 自己的 `A2AClientError` 异常层级(AgentCard 解析失败时 SDK 抛出的是后者,原始信息形如 "Network communication error fetching agent card ... All connection attempts failed"),再转换成带解决方法的提示——对远程服务的错误做"翻译"是写 A2A client 的一个基本功。

### EXCEL_AGENT_USE_LLM=false(离线模式)

```bash
EXCEL_AGENT_USE_LLM=false python -m services.excel_agent
```

启动日志会显示(已实测校验):
```
ExcelNarrator: per-sheet LLM summaries DISABLED (deterministic templates only).
```

此时 Agent 完全离线工作:每个 sheet 只生成确定性的模板文本(表头段落 + 逐行一句话),不会尝试连接 Azure AI Foundry。所有 sheet 之后还有确定性的**列视图**(`EXCEL_AGENT_COLUMN_VIEWS=true`,默认开启):把每一列沿着各行再写一遍,例如 `Revenue — column 'CY2025 Q1', by Company: NVIDIA is 44062; AMD is 7438; ...`。逐行叙述会把同一列拆进很多分片,"沿一列往下读很多行"的问题(八家公司里谁的毛利率最低、十二个季度的平均值)有了列视图只需几个分片就能拿到整列。适合没有 GPT 部署、或者希望课堂演示 100% 可复现的场景。默认情况下(`EXCEL_AGENT_USE_LLM=true` 且 GPT-4 部署凭证齐全)启动日志会是:
```
ExcelNarrator: per-sheet LLM summaries ENABLED (Azure AI Foundry GPT-4 deployment).
```
即使启用了 LLM,单次调用失败也会自动降级为确定性模板(best-effort 设计),不会让整个 narration 任务失败。

### 端口冲突

如果 9999 端口已经被占用(比如忘记关掉上一次的 Agent 进程,或者参考项目 multiagent-rag 的 Agent 还在运行),再次启动会看到类似下面的报错(Linux 上是 Errno 98,macOS 上是 Errno 48):
```
ERROR:    [Errno 98] error while attempting to bind on address ('127.0.0.1', 9999): address already in use
```
解决方法:用 `ss -ltnp | grep 9999`(或 `lsof -i :9999`)找到并结束占用该端口的进程,或者用 `--port` 换一个端口(同时要让 `.env` 里的 `A2A_EXCEL_AGENT_URL` 指向新端口)。MCP server 的默认端口(8100)、FastAPI 的端口(8010)如果冲突,处理方式相同。

## 课后延伸问题

1. 为什么 `services/excel_agent` 从不 `import` 主项目 `config` 或 `src` 里的任何代码,反过来 `A2AExcelClient` 也从不 `import services.excel_agent`?如果去掉这层约束、直接互相调用函数,会失去什么、又能多得到什么?
2. A2A 和 MCP 看起来都是"连接另一个东西"的协议,它们的边界具体在哪里?什么情况下你会选 MCP、什么情况下你会选 A2A?(提示:想一想"给 LLM 一个可调用的工具"和"把一整个任务委托给另一个自治的、可能要跑很久、自己维护状态机的 agent",这两者本质上有什么不同。)
3. `ask_rag` 被设计成一个 MCP tool,`rag://index/status` 被设计成一个 resource——如果反过来设计(索引状态做成 tool、问答做成 resource),会带来什么问题?这背后判断"该用 tool 还是 resource"的一般原则是什么?
4. Demo 1 里,Excel Narrator Agent 返回的 artifact 同时包含叙述文本和结构化 sheet 元数据(text part + data part)。如果这个 Agent 以后要支持第三种文件格式(比如 `.json`),主项目的 `main.py` 或 `TextChunker` 需要跟着改吗?为什么?
5. 如果要给这个 MCP server 新增一个"删除某个已索引文档"的能力,你会把它设计成 tool 还是 resource?为什么?
