# DeepResearch

DeepResearch 是一个可本地运行的 AI 深度研究应用。输入一个研究主题后，它会拆分研究任务、搜索公开资料、读取网页和 PDF 正文、按顺序分析提取到的全文，并生成带来源引用的 Markdown 报告。研究进度、来源记录和恢复点保存在本机。

**技术栈：** Vue 3、TypeScript、FastAPI、SQLite、OpenAI 兼容的模型 API。

## 功能

- **分解研究任务：** 将宽泛主题拆成可独立执行或有依赖关系的子任务。
- **多渠道搜索：** 支持 DuckDuckGo、Tavily 和备用网页搜索渠道，并可使用 arXiv、OpenAlex、Semantic Scholar、Crossref 等学术来源。
- **读取和分析全文：** 提取公开网页与 PDF 正文，按完整文本分块分析；区分全文、部分正文与仅搜索摘要等状态。
- **引用与来源追踪：** 汇总去重来源、报告引用、来源读取状态和相关资料。
- **后台运行与恢复：** 通过实时进度查看研究状态；支持停止、历史记录和从已保存的任务进度继续。
- **报告导出：** 支持 Markdown、纯文本、HTML、DOCX 和 PDF。

## 运行要求

- Python 3.10 或更高版本
- Node.js 22 或更高版本
- 可调用的 OpenAI 兼容模型服务及其 API Key
- 可选：Tavily API Key；没有 Tavily Key 时可使用无需专属密钥的公开搜索渠道

## 本地部署

### 1. 获取代码并创建 Python 环境

```bash
git clone https://github.com/WL-Y/DeepResearch.git
cd DeepResearch

conda create -n deepresearch python=3.11 -y
conda activate deepresearch
python -m pip install -r backend/requirements.txt
```

也可以使用已有 Python 虚拟环境安装 `backend/requirements.txt`。

### 2. 配置模型与搜索渠道

在项目根目录执行：

```bash
cp backend/.env.example backend/.env
```

Windows PowerShell 可用：

```powershell
Copy-Item backend/.env.example backend/.env
```

编辑 `backend/.env`，至少设置模型服务的 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`。例如，使用 DeepSeek 的 OpenAI 兼容接口时，按服务商当前文档填写对应的 URL、模型 ID 和密钥。

可按需配置搜索渠道：

- `SEARCH_ENGINES`：主网页搜索渠道，例如 `duckduckgo` 或 `duckduckgo,tavily`。
- `TAVILY_API_KEY`：使用 Tavily 时填写；不使用时无需配置。
- `SEARCH_FALLBACK_ENGINES`：备用网页搜索渠道列表。
- `ACADEMIC_SEARCH_ENGINES`：学术搜索渠道列表。

所有可用选项和默认值见 [backend/.env.example](backend/.env.example)。**不要把真实 API Key 写入示例文件、提交记录或 Issue；`backend/.env`、研究数据库、缓存和运行记录均属于本地数据。**

### 3. 启动后端

在项目根目录打开一个终端：

```bash
cd backend
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000
```

后端 API 文档：<http://127.0.0.1:8000/docs>

### 4. 启动前端

在另一个终端执行：

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

打开 <http://127.0.0.1:5174>。本地服务默认绑定回环地址，不会监听局域网网卡。

## 研究数据与配置

- API 与模型配置：`backend/.env`（从 `.env.example` 复制创建）
- SQLite 数据库、研究报告、恢复点：`backend/workspace/`
- 搜索与正文缓存：`backend/cache/`
- 运行时配置说明：`backend/.env.example`

数据库、缓存、日志、密钥和研究内容不会作为项目源代码提交。需要备份时，请自行安全保存 `backend/workspace/`。

## 开发与验证

后端测试（在项目根目录运行）：

```bash
cd backend
python -m unittest discover -v
```

前端测试和生产构建：

```bash
cd frontend
npm test
npm run build
```

## 项目结构

```text
backend/
  src/                 FastAPI、研究调度、搜索、正文读取、分析、报告与导出
  test_*.py            后端测试
  requirements.txt     Python 依赖
  .env.example         不含密钥的配置模板
frontend/
  src/                 Vue 界面、研究状态、来源详情与导出
  tests/               前端测试
  package.json         前端脚本和依赖
  package-lock.json    锁定的前端依赖
*.md                   检索、预算、提示词、性能与部署文档
```

更多实现说明：

- [本地部署](LOCAL_DEPLOYMENT.md)
- [检索、全文读取与故障恢复](RETRIEVAL.md)
- [模型提示词与报告规则](PROMPTS.md)
- [调用预算与恢复机制](COST_CONTROL.md)
- [执行性能记录](PERFORMANCE.md)
- [贡献指南](CONTRIBUTING.md)
- [社区行为准则](CODE_OF_CONDUCT.md)
- [安全漏洞报告](SECURITY.md)

## 贡献

欢迎通过 GitHub Issues 报告问题，或提交 Pull Request。请勿在 Issue、日志或截图中发布 API Key、个人研究报告或其他敏感信息。安全问题请按 [安全策略](SECURITY.md) 私下报告。

## 开源协议

本项目使用 [MIT License](LICENSE)。
