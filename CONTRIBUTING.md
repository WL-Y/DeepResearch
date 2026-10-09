# 贡献指南

感谢你参与 DeepResearch。提交改动前，请先搜索已有 Issue，避免重复工作。较大的功能、接口变化或架构调整，建议先创建讨论 Issue，说明问题、使用场景和计划方案。

## 开发流程

1. Fork 仓库并从 `main` 创建功能分支。
2. 不要提交 `.env`、API Key、研究缓存、运行记录或模型输出。
3. 保持改动聚焦；同步更新相关测试与文档。
4. 运行后端测试及前端测试、构建。
5. 创建 Pull Request，写清问题、实现方式、验证结果和兼容性影响。

Pull Request 和推送到 `main` 的提交会通过 GitHub Actions 重复执行这些检查。

```powershell
# 后端
cd backend
python -m unittest discover -q

# 前端
cd ../frontend
npm test
npm run build
```

## 提交约定

推荐使用简洁的 Conventional Commits 风格：

- `feat:` 新功能
- `fix:` 问题修复
- `docs:` 文档更新
- `refactor:` 不改变外部行为的重构
- `test:` 测试调整
- `chore:` 工程维护

提交内容应便于审查。不要混入无关格式化、生成文件或个人环境配置。

## Pull Request 要求

- 说明用户可观察到的变化及必要背景。
- 列出实际执行的测试；未执行的检查需要说明原因。
- 涉及界面时可附截图；涉及配置时同步更新 `.env.example`。
- 不降低现有安全边界，不在日志、错误或前端响应中泄露密钥和完整内部提示。
- 项目维护者可要求拆分过大的 PR，或在合并前补充测试和迁移说明。

提交贡献即表示你同意按本仓库的 [MIT License](LICENSE) 发布该贡献，并遵守 [社区行为准则](CODE_OF_CONDUCT.md)。
