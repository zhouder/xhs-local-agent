# Windows 本地小红书个人号增长运营 Agent

本项目是一个本地、可暂停、可审计的运营辅助 MVP。第二阶段 A 已支持 Mock、DeepSeek、GLM 和通用 OpenAI-compatible AI 草稿生成；人工审核与 Playwright 仍然只有 dry-run。**不会点击真实发布，不会自动评论/私信/点赞，不读取或保存 cookie。**

架构、表设计、内部 Schema、风险边界和四阶段计划见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 1. 安装 Python

安装 Python 3.12（建议从 python.org 安装），安装时勾选 `Add Python to PATH`。验证：

```powershell
py -3.12 --version
```

## 2. 安装依赖

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

若 PowerShell 禁止激活脚本，可直接使用 `.\.venv\Scripts\python.exe` 执行后续命令。

## 3. 安装 Playwright 浏览器

默认使用本机 Microsoft Edge。Playwright 驱动仍需安装：

```powershell
python -m playwright install chromium
```

若没有 Edge，把 `config.yaml` 的 `browser.channel` 改为 `chromium` 或留空，并根据环境调整代码配置。

## 4. 配置 `.env`

通常不需要手工创建或编辑 `.env`。在“设置 → 新增供应商”中直接粘贴 API Key，系统会自动生成环境变量名并写入本地 `.env`。更新已有文件前会备份为 `.env.bak`，其他变量保持不变；两个文件都已被 `.gitignore` 排除。

页面只显示“已配置 / 未配置 / 不需要”，不会回显 Key。SQLite 仅保存环境变量名，不保存真实 Key。飞书等尚未提供 UI 的凭据仍可参考 `.env.example` 手工配置。

## 5. 启动本地服务

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

也可从任意当前目录运行 `D:\codex\workspace\xhs-growth-agent\run.ps1`；脚本会自动切换到项目目录，仅在依赖清单变化时安装依赖，并从 `config.yaml` 读取监听地址和端口。只检查环境而不启动服务：

```powershell
.\run.ps1 -Check
```

若系统执行策略阻止本地脚本，可使用 `powershell -ExecutionPolicy Bypass -File .\run.ps1`。数据库首次启动时自动创建在 `data/xhs_agent.db`。

## 6. 打开本地控制台

浏览器访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。服务默认不监听局域网地址。

## 7. 手动登录小红书

只有点击已批准草稿的 `Playwright dry-run 填表` 时才会打开临时 Edge 窗口。请在该窗口手动登录。程序创建非持久化 context，不调用 cookie 读取/导出 API；窗口关闭后会话不保存。

## 8. 生成草稿

进入“设置”选择默认 Provider，再进入“草稿”。生成参数包括主题、目标人群、内容风格、字数范围、观点讨论型标题、偏科普和偏涨粉表达。草稿写入 SQLite，并产生 AI 调用与 `note.created` 审计记录。

真实 Provider 必须返回内部 `NoteContent` JSON Schema。系统会去除纯 JSON Markdown fence；JSON、Schema、字数或安全校验失败时，自动发起一次修复请求。第二次仍失败则终止并写审计，不保存不合格草稿。

本地安全层会拦截医疗、法律、金融和政治敏感建议，以及诱导关注、虚假承诺、夸大收益、互关、刷量、私信领取和评论区口令等内容。模型自报安全不能绕过本地检查。

## 极简 AI 供应商配置

1. 打开“设置”，点击“新增供应商”。
2. 可先从 OpenAI、DeepSeek、Qwen、Kimi、GLM、豆包、OpenRouter、SiliconFlow、Claude、Gemini、Ollama 或 LM Studio 快速填充。
3. 核对供应商名称、Base URL 和 API 格式。
4. 直接粘贴 API Key；Mock、Ollama 和本地 LM Studio 可留空。
5. 在“模型列表”中每行填写一个模型 ID。型号是自由文本，不受 preset 限制。
6. 从下拉框选择默认模型；只有一个模型时会自动选中。
7. 保存后点击“测试连接”，成功后可设为默认供应商。

普通配置页只显示三种 API 格式：

- **Chat Completions (`/chat/completions`)**：适用于 DeepSeek、Qwen、Kimi、GLM、OpenRouter、SiliconFlow 等多数 OpenAI-compatible 平台。OpenAI 也可选此格式。
- **Anthropic Messages (`/v1/messages`)**：通常用于 Claude 或兼容 Claude Messages 的网关。
- **Responses (`/responses`)**：用于 OpenAI Responses API 或兼容接口。

Base URL 填写平台 API 根地址即可，例如 `https://api.deepseek.com` 或 `https://api.example.com/v1`。系统会根据格式追加正确路径；如果已经包含 `/chat/completions`、`/v1/messages` 或 `/responses`，不会重复追加。

旧版 Mock、Ollama、Gemini、Custom HTTP 等 Profile 不会被删除，在列表中标记为“旧版 / 高级 Provider”，但不会出现在普通 API 格式下拉框。

每个供应商的多个模型保存在 `provider_models` 表，`ai_providers.default_model_id` 指向默认模型。旧版 `model_id` 会在启动时幂等迁移为模型列表，不破坏现有 Provider。生成草稿和连接测试默认使用选中的默认模型。

“高级设置”默认折叠，包含 JSON mode、Streaming、Vision、Tools、超时、最大输出 Tokens、温度和扩展 JSON。普通用户无需修改。认证头和 API Key 不允许放入高级 JSON；真实 Key 仅保存在本机 `.env`，写入前备份 `.env.bak`，页面、SQLite 和审计日志均不显示明文。已配置的 Key 输入框显示 `******** 已配置，留空则不修改`，这只是状态提示，不是 Key 回显。

编辑页会预览 API 格式、默认模型、最终请求 URL 和 Key 状态。点击“保存并测试连接”时会先保存当前表单，再发送 `请只返回 JSON：{"ok": true}`，完成后继续停留在编辑页。成功显示具体模型；失败显示脱敏后的原因并写入审计：

- `401 Unauthorized`：检查 API Key。
- `404 Not Found`：检查 Base URL 或 API 格式。
- `400 Bad Request`：检查模型 ID 和请求参数。
- 请求超时：检查网络或 Base URL。
- 返回无法解析：检查接口是否与所选 API 格式兼容。

默认供应商不能删除，必须先选择另一个默认供应商。

## 9. 审核并发布

打开草稿，核对并编辑标题、正文、话题、封面提示词和素材绝对路径。点击“提交审核”后进入 `pending_review` 并弹出 Windows 通知。只有显式点击“批准发布”才进入 `approved`。

第一阶段的“真实发布”按钮禁用。`Playwright dry-run` 只打开页面、等待手动登录、填表和截图，绝不点击发布。dry-run 不会把状态伪装成 `published`。

## 10. 启用或关闭自动评论

第一阶段未实现自动评论，记录页仅为后续数据展示预留。后续启用时先在 `config.yaml` 设置 `interaction.enabled`，并受时间窗口、每日上限、关键词和暂停状态约束。

## 11. 启用或关闭兴趣互动

第一阶段未实现自动兴趣互动。默认关键词和限额已放入 `config.yaml`，后续任务必须逐次经过 `PolicyEngine`。Dashboard 的“暂停 agent”会阻断所有策略动作。

## 12. 配置飞书命令通道

第一阶段仅提供命令 parser 和数据库表，不开放公网 endpoint。后续将从 `.env` 读取 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN`。`/approve <note_id>` 只能改变审核状态；`/publish_now` 还需要二次确认，不能绕过审核。

## 13. 查看审计日志

控制台“审计日志”展示最近 500 条记录。浏览器错误在“浏览器错误”页；截图位于 `data/screenshots`。每次 AI 失败、状态转换、策略阻断和浏览器动作都会留下记录。

## 14. 常见错误与排查

- `API key is required`：当前 provider 不是 mock；在 `.env` 设置对应 key，或在设置页切回 `mock`。
- `invalid structured output after one repair attempt`：模型连续两次未满足 JSON Schema、字数或安全要求；查看审计日志后调整主题或模型。
- Playwright 找不到浏览器：运行 `python -m playwright install chromium`，确认 `browser.channel` 与本机匹配。
- 等待标题框超时：先完成手动登录；页面选择器可能变化，只修改 `app/browser/selectors/xhs.yaml`。
- 素材上传失败：使用存在且当前用户可读的绝对路径。
- toast 不显示：检查 Windows 通知权限；失败只记日志，不会改变审核结果。
- `human_approval_required`：草稿必须先提交审核并明确批准。
- SQLite 被占用：确认没有多个开发服务器同时使用同一数据库。

## 第一阶段人工验收清单

- 启动 `run.ps1`，确认控制台只监听 `127.0.0.1:8765`。
- 生成草稿，刷新页面后确认内容仍存在。
- 编辑草稿并绑定一张实际存在的本地图片。
- 提交审核，确认状态为 `pending_review` 且 Windows 通知出现。
- 在待审核状态确认没有 dry-run 或真实发布入口。
- 批准后再次编辑，确认状态自动回到 `draft`，必须重新审核。
- 再次批准并运行 dry-run，手动登录后确认只填表、不点击发布。
- 暂停 agent，确认已批准草稿也被 PolicyEngine 阻止。
- 查看审计日志和浏览器错误页，核对操作、失败原因和截图路径。
- 关闭服务后确认没有持久浏览器用户目录、cookie 导出文件或真实互动记录。

## 测试

```powershell
python -m pytest -q
```

测试完全离线，通过模拟 HTTP 响应覆盖 DeepSeek、GLM 和 JSON repair，不依赖真实小红书账号、浏览器登录或 AI key。
