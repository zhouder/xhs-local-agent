# XHS Local Agent

Windows 本地运行的小红书个人号内容生产与安全发布 agent。当前阶段支持 AI Provider 配置、草稿生成、内容计划、批量生成、人工审核、Playwright 填写发布页、截图等待最终确认、飞书/本地命令 MVP、审计日志和安全验收脚本。

安全边界：默认 `dry_run`；不自动评论、不自动私信、不自动点赞、不做兴趣浏览；不读取、导出或保存 cookie；小红书登录必须手动完成；真实点击发布按钮必须先经过草稿审核和最终确认。

## 启动

```powershell
cd D:\codex\workspace\xhs-growth-agent
.\run.ps1 -Check
.\run.ps1
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

## AI Provider

进入“设置”新增或选择默认 Provider。API Key 只写入本地 `.env`，SQLite、页面和审计日志只保存环境变量名和配置状态。

OpenModel 推荐配置：

- Base URL: `https://api.openmodel.ai`
- API 格式: `Anthropic Messages (/v1/messages)`
- 认证方式: `auto`，实际对 OpenModel 使用 Bearer Token
- 模型: `deepseek-v4-flash`

## 内容生产

单篇生成：进入“草稿”，填写主题、目标人群、内容风格、字数范围和偏好，生成后状态为 `draft`。

批量生成：进入“内容计划”，创建计划并按行输入主题，再点击“批量生成草稿”。每个主题会调用默认 AI Provider，生成的笔记仍保存为 `draft`，不会自动提交审核。

## 审核与发布模式

状态机核心路径：

`draft -> pending_review -> approved -> publishing -> waiting_final_confirm -> publish_uncertain/published/cancelled/returned_to_edit`

发布模式：

- `dry_run`: 默认模式。可打开页面、填表、截图，但不会点击发布按钮。
- `fill_only`: 打开发布页，手动登录后真实填写标题、正文、话题和图片，保存截图，进入 `waiting_final_confirm`，不点击发布。
- `publish_after_final_confirm`: 先填表截图并进入 `waiting_final_confirm`；只有点击“最终确认并发布”后才会重新打开页面、重新填表并点击发布按钮。

最终确认后，系统默认标记为 `publish_uncertain`，要求人工在小红书页面核验结果；不要盲目标记 `published`。

## 素材管理

在草稿详情页的“本地图片素材路径”中每行填写一个本地图片路径。支持 `png/jpg/jpeg/webp`，最多 9 张。路径不存在或格式不支持会阻止发布并写入审计日志。没有图片时允许纯文本流程，页面会明确提示。

## 飞书/命令 MVP

本地测试 endpoint:

```powershell
curl -X POST http://127.0.0.1:8765/commands/mock -F "command=/status"
```

命令：

- `/status`
- `/drafts`
- `/pending`
- `/approve <note_id>`
- `/reject <note_id>`
- `/waiting`
- `/final_confirm <note_id>`
- `/cancel <note_id>`
- `/pause`
- `/resume`

`/approve` 只会把 `pending_review` 改为 `approved`，不会发布。`/final_confirm` 只允许 `waiting_final_confirm` 且已有填表截图的笔记执行。

## 验收脚本

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py
.\.venv\Scripts\python.exe scripts\smoke_ai_provider.py
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py
.\.venv\Scripts\python.exe scripts\smoke_review_flow.py
.\.venv\Scripts\python.exe scripts\security_scan.py
.\.venv\Scripts\python.exe -m pytest -q
```

`check_xhs_selectors.py --open-page` 只打开页面并检查选择器，不填表、不发布。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [安全边界](docs/SAFETY_BOUNDARIES.md)
- [发布流程](docs/PUBLISH_FLOW.md)
- [飞书命令](docs/FEISHU_COMMANDS.md)

## 不实现项

当前仍禁用：自动评论、自动私信、自动点赞、兴趣浏览、风控绕过、验证码绕过、反检测、cookie 导出/保存、批量刷量、批量骚扰。
