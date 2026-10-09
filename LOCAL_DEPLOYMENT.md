# 本地运行

项目路径：`/home/wwww/VSCode/DeepResearch-main`

Conda 环境：`deepresearch-local`（Python 3.11、Node.js 22）。

## 模型配置

编辑 `backend/.env`，设置 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`。
当前保留示例中的模型服务地址与模型名；需按自己的模型服务填写有效配置。
搜索默认使用无需密钥的 DuckDuckGo。修改配置后重启后端。

## 启动

在两个终端中分别运行以下命令。如果服务已经运行，需要先停止旧进程以释放端口。

后端：

```bash
source /home/wwww/miniconda3/etc/profile.d/conda.sh
conda activate deepresearch-local
cd /home/wwww/VSCode/DeepResearch-main/backend
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000
```

前端：

```bash
source /home/wwww/miniconda3/etc/profile.d/conda.sh
conda activate deepresearch-local
cd /home/wwww/VSCode/DeepResearch-main/frontend
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

- 页面：http://127.0.0.1:5174
- 接口文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health

在对应终端按 Ctrl+C 停止服务。研究记录在 `backend/workspace/`，缓存位于 `backend/cache/`。
本次部署不配置开机自启，电脑重启后按上述命令启动。

## 本次启动的后台服务

本次已在后台启动服务，日志分别位于 `backend/local-server.log` 和 `frontend/local-server.log`。
进程号记录在 `/tmp/deepresearch-local-backend.pid` 和 `/tmp/deepresearch-local-frontend.pid`。
在本次运行期间，可分别停止后端或前端：

```bash
kill "$(cat /tmp/deepresearch-local-backend.pid)"
kill "$(cat /tmp/deepresearch-local-frontend.pid)"
```

填写模型密钥后，停止当前后端，再按上面的后端启动命令运行即可。前端无需重启。

## 依赖与验证

前端依赖按 `frontend/package-lock.json` 使用 `npm ci` 安装。
后端按 `backend/requirements.txt` 安装，另补装现有测试需要的 `httpx`。

```bash
# 在 deepresearch-local 环境、项目根目录中运行
python -m pip install -r backend/requirements.txt httpx
```
