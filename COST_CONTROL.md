# 成本控制与中断恢复

## 默认配置

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| LLM_TIMEOUT_SECONDS | 90 | 普通模型调用超时秒数 |
| LLM_MAX_RETRIES | 0 | 非研究上下文的 SDK 重试；研究预算上下文内始终关闭 SDK 重试 |
| LLM_MAX_INPUT_CHARS | 70000 | 单次系统提示和用户提示的字符上限 |
| LLM_MAX_OUTPUT_TOKENS | 5000 | 规划和摘要等调用的输出上限 |
| REPORT_MAX_OUTPUT_TOKENS | 12000 | 报告写作输出上限 |
| REPORT_MAX_INPUT_CHARS | 180000 | 报告请求（含系统提示、材料及续写草稿）的字符预算 |
| REPORT_TIMEOUT_SECONDS | 180 | 报告写作超时秒数 |
| RESEARCH_TOKEN_BUDGET | 1500000 | 同一研究累计模型 token 预算 |
| RESEARCH_MAX_MODEL_CALLS | 200 | 同一研究累计模型调用上限 |
| FULLTEXT_CHUNK_CHARS | 20000 | 顺序分块大小；后续块继续处理，不是来源总长度限制 |
| REPORT_TOKEN_BUDGET | 100000 | 报告写作阶段累计 token 预算 |
| REPORT_MAX_MODEL_CALLS | 2 | 一次写作，输出截断时最多一次续写；配置更大也不自动增加调用 |
| REPORT_MATERIALS_MAX_CHARS | 160000 | 报告笔记与来源目录的材料容量上限 |
| RESEARCH_TASK_CONCURRENCY | 2 | 独立子任务并行数，允许 1–2 |

正常完整报告只调用一次写作模型。证据校验、自动缺口补查、报告审校和有限质检已移除，不再产生相关模型调用。旧配置中的相关开关不再生效；旧 `REPORT_EVIDENCE_MAX_CHARS` 改为 `REPORT_MATERIALS_MAX_CHARS`。

## 通用报告生成流程

所有主题共用“本地整理材料 → 一次写作 → 本地整理引用与排版”流程。报告准备不再调用模型逐组压缩，也不要求模型反复输出含全部引用编号的压缩 JSON。

材料可用容量取配置上限、完整请求的字符余量、全研究剩余 token 和写作阶段剩余 token 的共同约束，同时预留输出和安全余量。系统提示、主题和 JSON 转义开销均参与检查；续写还计算已有草稿的开销。字符值是可调整的部署预算，不是某个主题或研究 ID 的专用阈值。

优先完整传入笔记，保留段落及表格。超容量时使用本地、确定性的段落选择：先覆盖各研究任务、各引用来源和每个任务的限制条件，再按轮次补充有引用、限制条件和定量信息的完整段落。不会切掉段落或表格的后半段。选择记录在 `report-input.json`，含完整/选段模式、保留和未纳入的段落数量、原始段落索引及实际写作材料；原始全文和笔记保留，选段不能被宣称为全部笔记。如果最低覆盖本身放不进预算，在付费请求之前给出具体原因。

报告写作计入 `report_write`，正常一次，只有输出截断允许一次续写；API 超时或其他异常不自动重试。历史压缩的 `report` 阶段和历史 token/调用总量仍保留并计入全研究预算，旧 `report-materials.json` 不重写。两阶段分开记账，防止旧压缩耗尽最终写作额度。已完成草稿恢复直接复用，未完成草稿从保存位置续写，避免从头再生成。

这保证调用数量和材料处理可审计，不保证任意大小的研究都能在任意小的预算内生成，也不能替代对模型事实和引用质量的核查。

## 精读调用优化（2026-10-09）

默认开启 `READING_OPTIMIZATION_ENABLED`。获取正文后，每个任务最多用一次批量筛选调用选择直接相关的来源；无效筛选结果使用本地排序，不额外请求模型修复。筛选决定持久化，恢复同一任务时复用。

## 搜索渠道与空结果恢复

通用网页检索默认使用 DuckDuckGo；主渠道完全无结果时，按顺序尝试 Yahoo、Brave、Google、Mojeek、Startpage，最多五个。它们由 `ddgs` metasearch 适配，连接不同上游索引，无需额外 API 密钥；当前已安装版本支持 Mojeek 和 Startpage。Tavily 需配置自己的 API Key 后才能使用；arXiv、OpenAlex、Semantic Scholar 是学术论文源，依 `SEARCH_PROFILE` 路由，不替代一般企业、产业或政策新闻搜索。

精确查询无结果后，系统用研究主题与子任务标题构造一次更宽的本地查询并保留年份，再由返回空结果的渠道检索。查询失败时保留渠道名、标准类别、异常类型和已脱敏的短原因；速率限制、访问拒绝和网络错误不以改写查询绕过冷却。任务恢复只重跑未完成子任务并复用已保存结果。

渠道可在 `SEARCH_ENGINES`、`SEARCH_FALLBACK_ENGINES` 与 `ACADEMIC_SEARCH_ENGINES` 配置。完整引擎列表见 [ddgs 官方项目说明](https://github.com/deedy5/ddgs#engines)。

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| ANALYSIS_SOURCE_LIMIT | 4 | 普通任务最多新选取的精读来源数 |
| ANALYSIS_PART_LIMIT | 16 | 普通任务新读正文块预算 |
| CONCEPT_SOURCE_LIMIT | 2 | 标题含概念、定义、术语等任务的来源上限 |
| CONCEPT_PART_LIMIT | 8 | 概念任务新读正文块预算 |

超出块预算的整篇资料不进入本次精读，保留候选和未选择原因，不截取前半篇冒充全文。已开始付费分析的旧来源继续保留进度，可能超过新上限。上述是新读正文块预算，不是总 API 次数上限：筛选、长笔记汇总、输出截断后的拆分及最终写作仍可能调用模型。

同一研究中，对正文快照及模型、提示词、分块、思考配置一致的来源复用中立全文笔记，避免多个任务重复阅读。仅覆盖验证通过且没有缺失段落的笔记可复用；旧任务专属摘要不提升为跨任务缓存。较短的分块笔记直接完整拼接，不再额外调用模型汇总。选择与复用状态可在来源详情查看。

## DeepSeek 官方接口（2026-10-09 核对）

使用 OpenAI SDK 的 Chat Completions 接口，地址 `https://api.deepseek.com`，默认模型 `deepseek-flash`，显式 `stream=false`。默认通过 `extra_body.thinking.type=disabled` 关闭思考，且不发送会重新启用思考的 `reasoning_effort`。

启用 `DEEPSEEK_THINKING=true` 后，发送 `DEEPSEEK_REASONING_EFFORT`（`low`、`high` 或 `max`，默认 `high`），同时省略思考模式下无效的 temperature。对象形式的查询、筛选和摘要任务使用 `response_format=json_object` 并在提示中要求 JSON；规划仍输出数组，报告仍输出 Markdown。保留本项目的输出预算，不因接口支持更大输出而扩大消耗。

只有 `finish_reason=stop` 的非空正文作为正常结果。`length` 进入既有截断处理；`aborted`、资源不足、过滤等状态报错且不采用残文。收到 usage 后先记账，再检查结束状态，记录缓存命中及思考 token。研究上下文内禁用 SDK 隐式重试。

参考：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

## 预算记账规则

每次请求前预留估计输入 token、输出上限及安全余量，收到实际 usage 后结算。预算检查因此可能在实际用量尚未达到上限时拒绝下一次调用。

状态保存在 `workspace/runs/<research_id>/cost.json`。恢复沿用历史 token、调用次数和报告阶段用量，不重置额度。超时、缺少 usage 或进程异常退出时保守保留预留；连接失败按现有逻辑释放该次预留。预算记录损坏时停止，不自动清零。

## 摘要与报告恢复

- 全文按顺序分块分析，每块独立保存结果，然后按来源汇总。长文分析记录继续分层汇总，后续部分不会被每来源字符上限截掉。旧 SUMMARY_SOURCE_MAX_CHARS 已移除；FULLTEXT_CHUNK_CHARS 控制单次大小。全文流程每次处理一个来源块，兼容批处理最多四项且按输入大小拆批。
- 恢复时复用已完成分块与汇总结果；正文快照按内容摘要独立保存，避免 URL 缓存刷新后混用版本。旧选段摘要不能作为全文完成结果复用，恢复旧研究时会重新分析可用全文，但仍沿用原预算账本。
- 默认预算为 150 万 token、200 次调用，是上限而非固定消耗；用于容纳多篇长文的分块与汇总。达到上限仍保存进度并停止新调用，来源详情保留实际完成字符数，不标记为全文已完成。
- 报告在 `report-state.json` 保存草稿。写作输出截断时保存 `partial_draft`，最多续写一次；续写次数跨恢复累计。再次截断保留材料并中断，不把残稿当完整报告交付。
- 旧版报告状态文件中的草稿和续写位置可以迁移复用；已移除的审校状态不再执行。历史报告文件和旧审计记录不批量删除或改写。
- 同步外部调用无法强制终止；取消后若仍有请求在退出，须等待它退出才能继续，避免并发写入。

## 验证

在 backend 目录执行 `python -m unittest discover -q`。这些测试使用本地模拟，不访问模型或发起真实研究。
