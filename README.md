# XHS Local Agent

Windows 本地运行的小红书内容生产与安全发布 agent。当前版本支持 AI Provider、内容计划、批量生成草稿、人工审核、三种小红书发布类型、本地 dry_run 预览、Playwright 安全填表、最终确认、审计日志和验收脚本。

安全边界不变：不自动评论、不自动私信、不自动点赞、不做兴趣浏览；不绕过验证码、风控或反检测；不读取、导出、打印或保存 cookie；真实发布必须经过人工审核和最终确认。

## 启动

```powershell
cd D:\codex\workspace\xhs-local-agent
.\run.ps1 -Check
.\run.ps1
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

## AI Provider

在“设置”里选择默认 AI Provider。API Key 只写入本地 `.env`，不会明文展示在页面、SQLite 或审计日志里。

OpenModel 推荐配置：

- Base URL: `https://api.openmodel.ai`
- API 格式: `Anthropic Messages (/v1/messages)`
- 认证方式: `auto`
- 模型: `deepseek-v4-flash`

## 发布模式

- `dry_run`: 纯本地模拟，不打开小红书，不打开浏览器，不访问 `creator.xiaohongshu.com`，不上传素材，不点击发布。它会校验状态、内容、话题和素材，并生成本地 HTML 预览和 1080x1440 PNG 预览图。
- `fill_only`: 打开小红书发布页。用户手动登录后，系统会按草稿发布类型进入对应页面，上传素材或执行文字配图，再填写标题、正文和话题，截图后停在 `waiting_final_confirm`，不点击发布。
- `publish_after_final_confirm`: 先按 `fill_only` 填表截图。只有用户在最终确认页点击“最终确认并发布”后，才允许点击发布按钮。无法确认成功时标记为 `publish_uncertain`。

默认浏览器是 Chrome。可以在“设置 -> 浏览器选择”切换 Chrome / Edge / Chromium。调试阶段默认 `browser.keep_open_on_error: true`，选择器失败或登录超时时浏览器会保留，便于人工检查。

## 发布类型

当前只支持三种发布类型，草稿详情页“发布类型”下拉框可切换：

- `video_upload` 视频笔记：上传视频。目标 URL 为 `https://creator.xiaohongshu.com/publish/publish?from=menu&target=video`。
- `image_upload` 图文笔记：上传自己的图片。目标 URL 为 `https://creator.xiaohongshu.com/publish/publish?from=menu&target=image`。
- `image_text_to_image` 图文笔记：使用小红书“写文字 / 文字配图”。目标 URL 为 `https://creator.xiaohongshu.com/publish/publish?from=menu&target=image`。

暂不支持写长文、发播客。无图片草稿不会再自动进入 `target=article`，默认按 `image_text_to_image` 处理。

## 登录流程

`fill_only` 和最终确认发布使用专用浏览器 profile：`data/browser-profiles/chrome`。用户手动登录一次后，后续尽量复用浏览器自身登录态。代码不会调用 cookie 读取或导出 API。

如果打开的是登录页，请在浏览器里扫码登录。登录后系统会再次跳转到发布页，并等待发布页编辑器。如果 180 秒内找不到编辑器，页面会显示中文错误，`browser_errors` 会记录当前 URL、页面标题、步骤、选择器候选和截图。

## 素材管理

草稿详情页会按发布类型显示不同素材区域：

- 视频笔记：点击“添加视频”，支持 1 个 `mp4/mov`，保存到 `data/media/note-{id}/`。当前封面设置是 TODO。
- 图文笔记：点击“添加图片”，支持 1-9 张 `png/jpg/jpeg/webp`，保存到 `data/media/note-{id}/`。
- 文字配图：填写“文字配图内容 / 卡片文字”和风格偏好，不要求上传图片。小红书会把这段文字套入模板生成图片，不是 AI 绘图提示词。留空时系统会从标题和正文自动生成。

图片素材区支持：

- 点击“添加图片”后自动上传并刷新页面
- 一次选择多张图片
- 最多 9 张
- 缩略图网格预览
- 拖拽排序后保存顺序
- 删除图片后自动重排
- 生成 1080x1440 本地 AI 封面图

上传文件复制到 `data/media/note-{id}/`，不提交 GitHub。AI 封面由 Pillow 本地生成，使用系统字体和几何背景，不下载外部版权图片。

## fill_only 行为

- 视频笔记：打开【上传视频】页面，上传视频，等待处理完成或编辑区出现，再填写标题、正文、话题，截图后等待最终确认。
- 图文笔记：打开【上传图文】页面，先上传图片，等待缩略图/预览/编辑区出现，再填写标题、正文、话题，截图后等待最终确认。
- 文字配图：打开【上传图文】页面，进入【写文字】卡片编辑页，填写文字配图内容，点击【生成图片】，如有模板则选择默认模板并下一步，再填写标题、正文、话题，截图后等待最终确认。

## 最终确认页

dry_run 后会直接在最终确认页内嵌显示清晰 HTML 预览卡片，并附带 1080x1440 PNG 预览图。dry_run 状态下“最终确认并发布”按钮保持禁用。

fill_only 后最终确认页显示真实页面截图。只有状态为 `waiting_final_confirm` 且不是 dry_run，才允许最终确认发布。

## 选择器诊断

基础检查：

```powershell
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py
```

打开浏览器诊断：

```powershell
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target video
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-upload
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-text-to-image
```

`--open-page` 会使用同一个 Chrome profile 打开对应发布页，输出每个 selector key 的候选、命中情况、命中序号、元素 tag、placeholder 和文本摘要，并保存诊断截图。它不会上传文件、不会填写正文、不会发布、不会读取 cookie。兼容别名：`--target image`、`--target text2image`。

文字配图诊断默认只列出入口候选，不点击入口。需要验证入口点击时，显式加 `--click-entry`：

```powershell
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-text-to-image --click-entry
```

`--click-entry` 会先识别是否已经在“写文字”编辑页；如果已在编辑页，就直接检查卡片文字输入区和“生成图片”按钮，不再要求入口存在。需要点击入口时，会跳过上传图片、拖拽上传、`input[type=file]` 等候选；如果候选触发本地文件选择器，会立即报错并停止，避免误上传。

文字配图不是 AI 绘图 prompt。它是把一段短文字套进小红书模板生成图片；如果草稿里的“文字配图内容 / 卡片文字”留空，系统会从标题和正文自动生成一段卡片文字并保存回草稿。

完整文字配图诊断可以分阶段执行：

```powershell
# 只点击“文字配图”入口并检查是否进入写文字页
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-text-to-image --click-entry

# 点击入口，填入测试文字，并检查“生成图片”按钮；默认不会点击生成
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-text-to-image --test-flow

# 在 --test-flow 基础上才会点击“生成图片”
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-text-to-image --test-flow --click-generate

# 在已生成结果后才会点击“下一步”
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py --open-page --target image-text-to-image --test-flow --click-generate --click-next
```

这些诊断命令不会点击发布，不会读取、导出或保存 cookie。`--test-flow` 默认只填测试文字并检查按钮，只有加 `--click-generate` 才会生成文字图片，只有再加 `--click-next` 才会进入下一步。

## 内容计划与批量生成

入口：顶部导航“内容计划”。

1. 新建内容计划，填写名称、目标人群、内容风格、目标、主题列表、每天生成数量和发布时间段。
2. 进入计划详情页。
3. 使用“批量生成草稿”“只生成未生成主题”“重新生成失败主题”。
4. 生成后的草稿仍为 `draft`，不会自动提交审核。

## 验收脚本

```powershell
.\.venv\Scripts\python.exe scripts\check_env.py
.\.venv\Scripts\python.exe scripts\smoke_ai_provider.py --provider mock
.\.venv\Scripts\python.exe scripts\check_xhs_selectors.py
.\.venv\Scripts\python.exe scripts\smoke_review_flow.py
.\.venv\Scripts\python.exe scripts\security_scan.py
.\.venv\Scripts\python.exe -m pytest -q
```

## 文档

- [架构](docs/ARCHITECTURE.md)
- [安全边界](docs/SAFETY_BOUNDARIES.md)
- [发布流程](docs/PUBLISH_FLOW.md)
- [飞书命令](docs/FEISHU_COMMANDS.md)

## 仍保持禁用

自动评论、自动私信、自动点赞、兴趣浏览、验证码绕过、风控绕过、反检测、cookie 导出或保存、批量刷量、批量骚扰。
