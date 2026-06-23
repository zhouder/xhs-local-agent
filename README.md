# XHS Local Agent

Windows 本地小红书内容生产与安全发布 agent。当前支持 AI Provider、内容计划、批量生成草稿、人工审核、图片素材管理、Playwright 安全填表、最终确认、命令 MVP、审计日志和验收脚本。

安全边界不变：不自动评论、不自动私信、不自动点赞、不做兴趣浏览；不绕过验证码、风控或反检测；不读取、导出、打印或提交 cookie；真实发布必须经过审核和最终确认。

## 启动

```powershell
cd D:\codex\workspace\xhs-growth-agent
.\run.ps1 -Check
.\run.ps1
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

## AI Provider

在“设置”里配置默认 AI Provider。API Key 只写入本地 `.env`，不会明文显示在页面、SQLite 或审计日志。

OpenModel 推荐配置：

- Base URL: `https://api.openmodel.ai`
- API 格式: `Anthropic Messages (/v1/messages)`
- 认证方式: `auto`
- 模型: `deepseek-v4-flash`

## 发布模式

- `dry_run`: 纯本地模拟，不打开小红书，不打开 Edge/Chrome/Chromium，不访问 `creator.xiaohongshu.com`，不上传素材，不点击发布。它只做状态、内容、话题和素材校验，并生成本地模拟预览。
- `fill_only`: 才会打开小红书发布页。用户手动登录后，系统填写标题、正文、话题和图片，截图后停在 `waiting_final_confirm`，不点击发布。
- `publish_after_final_confirm`: 先按 `fill_only` 填表截图；只有用户在最终确认页点击“最终确认并发布”后才点击发布按钮。无法确认成功时标记为 `publish_uncertain`。

默认浏览器是 Chrome。可以在“设置 -> 浏览器选择”切换 Chrome / Edge / Chromium。Chrome 不可用时会显示中文错误，请手动切换，不会静默弹 Edge。

## 登录流程

`fill_only` 或最终确认发布会使用专用浏览器 profile：`data/browser-profiles/chrome`。用户手动登录一次后，后续尽量复用浏览器自身登录态。代码不会调用 `cookies()` 或 `storage_state()`，也不会导出 cookie。

如果打开的是登录页，请在浏览器里手动登录。登录后系统会继续等待发布编辑器并填表。若用户关闭浏览器、选择器失效或超时，页面会返回中文错误，并写入 `audit_logs` 和 `browser_errors`。

## 内容计划与批量生成

入口：顶部导航“内容计划”。

1. 在“新建内容计划”里填写计划名称、目标人群、内容风格、目标、主题列表、每天生成数量、发布时间段。
2. 创建后进入计划详情页。
3. 点击明显按钮：
   - `批量生成草稿`
   - `只生成未生成主题`
   - `重新生成失败主题`
4. 页面会显示总主题、成功/失败、每个主题的 `note_id` 和“查看草稿”入口。
5. 草稿列表支持按计划筛选。

生成后的草稿仍为 `draft`，不会自动提交审核。

## 话题规则

草稿保存时会保证话题不为空：

- `hashtags` 存储时不带 `#`
- AI 若把 `#tag` 写在正文末尾但 `hashtags` 为空，系统会从正文提取
- 提取不到时，根据标题和正文生成 3-6 个默认话题
- 编辑页支持中文逗号、英文逗号、空格、换行分隔
- 提交审核前至少保证 3 个话题

## 图片素材

草稿详情页的“图片素材”区域提供：

- `+ 添加图片`
- 多图上传
- 缩略图预览
- 拖拽排序并保存
- 删除图片
- 生成本地 AI 封面占位图

支持 `png/jpg/jpeg/webp`，最多 9 张。上传文件会复制到 `data/media/note-{id}/`，不会提交 GitHub。

在线图片建议不自动下载版权不明图片。MVP 只建议用户自行确认素材版权；如果以后接入下载，只允许用户配置的合法图片 API。

## 常见错误

- 浏览器被关闭：流程取消，重新点击 `fill_only`。
- 未登录：在打开的 Chrome profile 中手动登录。
- 选择器失效：检查 `app/browser/selectors/xhs.yaml`。
- 没有话题：保存草稿，系统会自动提取或生成。
- 没有素材：允许纯文本流程，也可以生成本地封面占位图。
- Chrome 启动失败：在设置里切换 Edge / Chromium。

## 验收脚本

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py
.\.venv\Scripts\python.exe scripts\smoke_ai_provider.py --provider mock
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py
.\.venv\Scripts\python.exe scripts\smoke_review_flow.py
.\.venv\Scripts\python.exe scripts\security_scan.py
.\.venv\Scripts\python.exe -m pytest -q
```

`check_xhs_selectors.py --open-page` 只打开页面并检查元素，不填表、不发布。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [安全边界](docs/SAFETY_BOUNDARIES.md)
- [发布流程](docs/PUBLISH_FLOW.md)
- [飞书命令](docs/FEISHU_COMMANDS.md)

## 仍保持禁用

自动评论、自动私信、自动点赞、兴趣浏览、验证码绕过、风控绕过、反检测、cookie 导出/保存、批量刷量、批量骚扰。
