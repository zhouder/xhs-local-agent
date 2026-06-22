# 第一阶段 MVP 验收审计

审计日期：2026-06-22。范围仅限第一阶段，不包含第二阶段功能开发。

## 逐项结论

1. **目录结构：通过。** `ai`、`browser`、`services`、`templates`、`static`、`tests`、`docs` 和 `data` 职责分离；选择器、配置、运行数据均有固定位置。
2. **README 启动步骤：通过。** 虚拟环境命令、依赖安装、Playwright、配置、启动和排错步骤与当前实现一致；`run.ps1 -Check` 已实际执行。
3. **run.ps1：通过（已修复）。** 脚本现在使用 `$PSScriptRoot`，可从其他目录运行；检测 Python；仅在 `requirements.txt` 哈希变化时安装；支持 `-Check`；服务地址从配置读取。
4. **.env.example：通过（已修复）。** 包含 DeepSeek、GLM、通用 OpenAI-compatible 和飞书所需变量；不包含真实值。
5. **config.yaml：通过。** 包含 app、browser、ai、publish、interaction、notifications、feishu 默认配置；默认 mock、仅监听 localhost、强制 review、dry-run 开启。
6. **SQLite 初始化：通过。** 实际初始化并检查 12 张要求的表；每张表都有 `id`、`created_at`、`updated_at`。
7. **Mock AI：通过。** 统一 Schema 验证通过；50 次重复生成稳定、确定且不依赖 API key。
8. **草稿状态机：通过（已加固）。** 七种状态均定义于集中转换表；非法转换抛错；ORM 拒绝未知状态；编辑或重新生成已审核内容会失效审核并回到 draft。
9. **发布流程：通过。** pending/rejected/draft 均不能进入浏览器发布；只有 approved 通过 PolicyEngine；dry-run 无任何 `click` 调用；`dry_run=False` 始终阻断并审计。
10. **选择器集中管理：通过。** Playwright 的 locator/wait selector 参数全部来自 `app/browser/selectors/xhs.yaml`；AST 测试持续校验。
11. **cookie 边界：通过。** 不存在 `cookies()`、`add_cookies()`、`storage_state`、`user_data_dir`；使用临时 `browser.new_context()`。
12. **真实互动：通过。** 不存在自动评论、私信、点赞执行代码；对应页面和表仅为后续阶段占位；浏览器模块没有 click 调用。
13. **审计日志：通过（已加固）。** AI 成功/失败、草稿变更、审核决策、暂停/恢复、通知、策略阻断、浏览器成功/失败及真实发布阻断均写入 audit log。
14. **失败截图和 browser_errors：通过（已修复）。** 页面存在时在关闭前截图；页面尚未创建时生成 `page-unavailable.png` 失败工件；两种情况都写 browser_errors 和 audit。
15. **测试覆盖：通过。** 覆盖 adapter、mock、安全分类、PolicyEngine、仓储、调度守卫、命令解析、审核、状态机、数据库表、配置契约、浏览器边界、失败截图和 CSRF。
16. **安全隐患：通过。** 无真实 key、无 key 日志、无应用代码绝对路径；`.env`、`.venv`、SQLite、截图均忽略；POST 带恶意跨域 Origin 时被阻断并审计。
17. **Windows 兼容性：通过。** PowerShell 启动脚本、Pathlib 路径、Windows toast 可降级；本机 Edge 已用 Playwright headless 实际启动。
18. **路径兼容：通过。** 应用使用 `pathlib.Path` 和 `ROOT`；URL 使用 `/` 是标准 URL，不是文件路径；未发现应用代码硬编码盘符。
19. **异常处理：通过。** AI、审核、浏览器和通知失败被转换为 HTTP 错误或记录后继续；Playwright 清理异常不会覆盖原始错误；单个请求失败不会终止 Uvicorn。
20. **人工验收：需要。** 自动测试不能替代 Windows toast 可见性、真实登录页面当前选择器、实际本地素材上传和人工确认“页面未点击发布”。清单见 README。

## 本次修复

- 审批后编辑/重新生成会清除 `approved_at`、失效 review queue 并回到 draft。
- 新增集中状态机和 ORM 状态枚举校验。
- 浏览器失败截图改为在 context 关闭前捕获；启动前失败也产生 PNG 工件。
- 真实发布禁用尝试、AI 成功/重新生成和通知增加独立审计。
- `run.ps1` 支持任意当前目录、依赖哈希和 `-Check`；Uvicorn 从 YAML 读取 host/port。
- 补充 OpenAI-compatible 环境变量和配置。
- 增加同源 POST 防护，阻止网页跨站触发本地控制台动作。
- README 增加执行策略说明和人工验收清单。

## 自动验收证据

- `python -m pytest -q`：43 passed。
- `run.ps1 -Check`：Environment check OK；第二次运行跳过依赖安装。
- Uvicorn 真实进程：Dashboard、notes、audit、settings、browser errors 均返回 HTTP 200。
- SQLite：12 张目标表全部存在。
- Playwright：本机 `msedge` channel 启动并访问 `about:blank` 成功。

## 结论

第一阶段可以进入人工验收。人工 checklist 完成前，不应声明真实小红书页面选择器和 Windows toast 的视觉效果已验收；真实发布仍保持禁用。
