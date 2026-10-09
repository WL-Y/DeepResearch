# 分层检索升级说明

## 实施内容

1. 正文读取：每次 search_many 默认最多新增 8 份成功正文，最多尝试 16 个未读候选，每个候选最多尝试 6 个公开入口。已读结果不占新增名额，新资料优先于旧失败资料；失败候选递补。原文仍受大小、时间、公开地址安全检查约束。
2. 获取与分析分离：完整提取的正文保存为独立快照，检索仍生成预览片段，但写作前的分析按顺序覆盖全部提取文本，默认每块 20,000 字符。`acquisition_status` 记录获取情况；`analysis_status`、`analyzed_chars` 和块计数记录模型实际处理进度。旧 `context_chars` 只是检索预览大小，不代表模型读完全文。
3. 学术检索：auto 根据查询中的学术信号路由，通用渠道继续保留。arXiv 与 OpenAlex 默认加入学术查询，Semantic Scholar 已停用。规则路由不是完整的主题分类模型，可用 academic/web 强制覆盖。
4. 来源处理：合并 DOI/arXiv/数据库 ID、清除跟踪参数，谨慎按完全相同标题及至少两名共同作者补充去重。冲突 DOI 不按标题自动合并。保留不同入口和实际读取 URL；同一论文的不同版本不合并正文或实验数字。
5. 排序：中英文词片段相关性初筛，最多 16 个候选进行一次语义重排，最多 30 秒且不重试；仅允许模型重排既有索引。失败回退，不允许新增 URL。具名方法保留优先阅读位置。域名和引文数量不能自动认证证据可靠性。
6. 引文追踪：依赖 Semantic Scholar，随该渠道一并停用，当前研究不会请求它的参考文献或被引接口。
7. 元数据：Crossref 每次最多核对 3 个已有 DOI，正式版本元数据单独保存，不将预印本重新标记为已同行评审，也不声明读到了全文。
8. 可追溯：搜索渠道、日期、版本、实际读取地址、读取状态和安全分类错误随检查点及摘要保存。GET /api/research/{id}/sources 返回有限公开字段，不返回全文和内部提示词；来源详情是检索候选清单，不等同于报告引用清单。
9. 故障恢复：arXiv 使用 8 秒连接、默认 30 秒读取超时，保持至少 3.1 秒请求间隔；Semantic Scholar 无密钥时采用更保守的请求间隔。同一渠道的请求、重试和冷却状态串行更新，429 的 Retry-After 同时支持秒数与 HTTP 日期。长时间冷却或重试耗尽时释放研究流程，让其他渠道继续。
10. 定向备用检索：学术接口超时、限流或服务异常后，通过已有通用搜索引擎检索相应论文站点，每个查询最多额外两次网页搜索。只保留域名、论文入口和主题相关性匹配的结果；失败不写成成功，不通过切换接口地址绕开限流。实际找到入口才记录 fallback_provider/fallback_results，后续按正常预算读正文。前端集中显示渠道提示及备用检索结果，不在每篇已读资料下重复同一错误；备用检索成功不代表原 API 已恢复。

## 配置

### 通用网页备用渠道（2026-10-09）

通用检索显式绑定 DDGS 后端；DuckDuckGo 不再使用随机的 `auto` 后端。当前提供 `duckduckgo`、`yahoo`、`brave_web`、`google`，Tavily 继续使用已有的密钥接入。公开网页渠道不需要额外 API 密钥，但可用性受其网页变化与访问限制影响；Brave 网页接入不同于官方 Brave Search API。可用后端在请求前检查，库未启用的后端不会静默切换为其他渠道。现有 DDGS 版本未启用 Bing，因此不将其列为已验证备用。

默认备用顺序为 Yahoo → Brave 网页 → Google，只有主渠道无结果或失败才触发，成功立即停止。每个查询最多尝试三个备用渠道；各渠道共享实例内的串行访问与冷却状态。网络错误/超时最多重试一次；限流/拒绝访问不立即重试，冷却 60 秒。检索过程不调用大模型。搜索缓存升级为 v4，避免复用旧版标错来源的自动后端结果，全文缓存与已完成笔记继续复用。

全部渠道无结果时，长查询允许一次本地简化：保留前四个检索项、明确年份/日期、引号短语、site 与排除条件；布尔表达式不改写。仅在此前返回空结果的最多两个通用渠道上尝试，不绕过故障或限流冷却。简化只用于候选发现，记录实际 matched_queries、discovery_query 和 query_relaxed，后续仍按完整原查询排序、筛选和读取。

错误按 no_results、timeout、network_error、rate_limited、access_denied、backend_unavailable 等分类。查询合并保留渠道类别和安全的异常类型，不保存含密钥/代理地址的原始异常；备用成功标记真实发现渠道，不声称原接口恢复。全部失败时，任务错误显示各渠道原因而不是笼统的英文“no results”。

配置：`SEARCH_FALLBACK_ENGINES`（留空关闭备用）、`SEARCH_FALLBACK_MAX_PROVIDERS`（0–3）、`SEARCH_WEB_TIMEOUT_SECONDS`（默认 10）、`SEARCH_WEB_RETRIES`（0–1）、`SEARCH_QUERY_RELAXATION`（默认 true）。参考 [DDGS 项目文档](https://github.com/deedy5/ddgs)、[Brave 官方 API 认证文档](https://api-dashboard.search.brave.com/documentation/guides/authentication)。

保留 backend/.env 中现有 LLM/Tavily 密钥，无需复制整个示例覆盖真实配置。新增配置可参考 backend/.env.example；未设置时采用代码默认值。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| SEARCH_PROFILE | auto | auto / web / academic |
| ACADEMIC_SEARCH_ENGINES | arxiv,openalex | 留空禁用学术渠道；Semantic Scholar 已停用 |
| ARXIV_READ_TIMEOUT_SECONDS | 30 | arXiv 单次响应读取超时，连接超时 8 秒；范围 12–90 |
| ACADEMIC_WEB_FALLBACK | true | 学术接口故障后定向检索论文入口，最多额外两次网页搜索 |
| SEMANTIC_SCHOLAR_API_KEY | 空 | 停用渠道的兼容配置，当前不会使用 |
| OPENALEX_API_KEY | 空 | OpenAlex 渠道启用时使用，注意账户预算 |
| CROSSREF_ENABLED | true | DOI 元数据核对开关 |
| CROSSREF_MAILTO | 空 | 可配置服务联系邮箱 |
| SEARCH_READ_BUDGET / SEARCH_READ_ATTEMPTS | 8 / 16 | 新增成功正文目标 / 候选尝试上限 |
| FULLTEXT_CHUNK_CHARS | 20000 | 全文顺序分块大小，无每来源累计字符截断 |
| SEARCH_RERANK_ENABLED | false | 开启会增加现有模型调用费用 |
| SEARCH_RERANK_CANDIDATES | 16 | 语义重排候选上限 |
| SEARCH_CITATION_EXPANSION | false | 依赖 Semantic Scholar，当前关闭 |
| SEARCH_EXPANSION_SEEDS / SEARCH_EXPANSION_RESULTS | 1 / 4 | 每次调用的种子数 / 每个种子线索数 |
| SEARCH_CACHE_TTL_SECONDS | 86400 | 普通查询缓存秒数 |
| SEARCH_FRESH_CACHE_TTL_SECONDS | 3600 | latest/最新等显式时效查询缓存秒数 |
| SOURCE_CACHE_TTL_SECONDS | 604800 | 解析正文缓存秒数，按查询重新选取节选 |
| SOURCE_MAX_BYTES / SOURCE_PDF_MAX_PAGES | 50000000 / 500 | 单文档下载字节及PDF页数安全上限 |
| SOURCE_MAX_EXTRACTED_CHARS | 5000000 | 缓存的单文档可读正文字符上限 |
| TAVILY_EXTRACT_FALLBACK / MAX_URLS | true / 5 | 直接读取失败后批量提取清洗正文；不冒充原始全文 |

当前保留 arXiv、OpenAlex 两个学术渠道及通用搜索，Semantic Scholar 和引文扩展均关闭。Tavily Extract 只在直接读取失败后调用，会消耗少量提取额度；需要完全禁用可设置 `TAVILY_EXTRACT_FALLBACK=false`。需要节省模型费用时关闭 SEARCH_RERANK_ENABLED。

搜索缓存按渠道独立保存：故障/空搜索不永久缓存，成功渠道结果仍可复用；旧版无期限缓存不会被新版读取，不删除原文件。正文缓存按地址、读取预算与解析版本隔离，不同查询复用下载后重新选取相关段落，保留原始抓取时间和部分读取标识；旧版查询节选不冒充新正文缓存。元数据核对缓存 7 天；引文关系缓存使用普通查询 TTL。执行调度和统计见 [性能说明](PERFORMANCE.md)。

## 测试

在 backend 目录执行（使用项目 Python 环境）：

```powershell
python -m unittest test_core test_evidence test_prompts test_resume test_export test_retrieval test_retrieval_recovery
python check_search_providers.py --live
```

第一条完全离线，无模型调用。第二条主动访问四个学术接口，每个进行少量请求，输出安全的状态和标题，不输出密钥，不执行完整研究。网络限制时按环境申请访问权限，不绕过认证或付费墙。

在 frontend 目录执行 npm test 和 npm run build；浏览器中打开历史报告并展开底部来源详情。旧记录缺少渠道信息时明确标为未记录，不伪造；没有检查点的旧报告显示空态，不影响报告阅读、导出和继续研究。

## 验收与边界

自动化测试验证：阅读预算、递补、缓存有效期、渠道故障回退、元数据解析、跨库及跨任务去重、语义排序格式限制、引文扩展上限、恢复与既有导出/引用行为。

上线质量还需人工对照同一组真实研究主题（RGB-D、机器人世界模型、技术选型、行业问题）：标注必须覆盖的文献与论断，比较相关候选比例、关键原文覆盖、数值论断证据支持、重复来源、耗时与调用费用。接口成功及单元测试通过不等于报告质量已经完成评估。

尚不包含：扫描 PDF 的 OCR、复杂版面表格重建、付费数据库授权全文、独立复现实验、自动撤稿结论判定。阅读器只在完成全部可解析页面或响应时记录全文已提取；报告仍只使用相关证据块。网页仅有摘要但正文结构相似时仍可能误判，需要继续提升文档类型识别。

## 官方接口参考

- arXiv：https://info.arxiv.org/help/api/user-manual.html
- Semantic Scholar：https://www.semanticscholar.org/product/api
- OpenAlex：https://help.openalex.org/api/
- Crossref：https://www.crossref.org/documentation/retrieve-metadata/rest-api/


## 正文完整性与截断恢复（2026-10-02）

获取状态与分析覆盖分开验证。`completeness_status` 记录 confirmed、unconfirmed、partial 或 failed；来源详情展示 `completeness_reason`。已有实质性章节、但无法确认结束结构的出版社页面标为 `body_text_unconfirmed`，保留正文供分析，不冒充全文，也不直接归为只有摘要。显式登录、订阅后继续阅读提示视为截断信号。

PDF 全部页面已遍历、但个别页无文本时，记录 `pdf_empty_pages` 并标为完整性未确认；无文本可能是空白、图片或扫描页，不能自动认定是下载截断，也不能声称内容已完整识别。页数、时间和字符上限仍标为部分获取。扫描 PDF 的页码标记不计入有效正文最低长度。正文缓存版本升级为 body-v3，既有按内容哈希保存的分析快照保留。

模型调用前核对文本哈希和连续分块覆盖。`analysis_completed_ranges` 保存已经完成的字符区间，`analysis_missing_ranges` 保存未完成区间，均采用从 0 开始、右端不包含的区间；前端转换为从 1 开始显示。`analysis_coverage_verified` 只表示全部提取文本已完成分块处理，不表示 OCR、图表理解或原始文档完整性已经确认。

单个正文块或汇总块输出截断时，按连续字符区间二分，最多细分 4 层，少于 2,000 字符的块不再二分。父块、子块结果均持久化，恢复时复用完成子块；同一来源的引用编号不会因拆块而增加。调用次数和 tokens 仍受原有研究预算约束，达到边界会停止并保留未完成区间。

实际报告生成使用严格材料打包：不再因 40,000 字符容量或预选 15 个来源而直接丢弃写作材料；最终报告引用展示仍沿用原有规则。材料超限时，按连续片段分组汇总，最多 3 轮，检查引用编号集合没有减少或增加。汇总仍可能压缩语义细节，不能保证模型保留每一事实；原始笔记和分组结果保留。`report-materials.json` 记录完成结果、重试及细分状态，恢复时可复用。若来源目录过大、汇总不合格或报告预算不足，则明确停止，不将容量截断后的材料当作完整输入。分组汇总使用现有模型，会占用原有报告阶段调用和 token 预算。

离线回归测试：`python -m unittest test_fulltext_reliability test_report_admission -q`。覆盖不标准论文结构、订阅提示、PDF 空页与扫描页、长文尾部、单块截断和恢复、覆盖校验、报告本地材料选择及引用保留。报告准备已改为零模型调用的通用容量适配，详见 COST_CONTROL.md；真实站点获取效果和模型摘要质量需另行实测。
