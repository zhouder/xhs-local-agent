from __future__ import annotations

from app.ai.base import AIProviderAdapter, coerce_generation_request
from app.ai.safety import validate_generation_request, validate_note_content
from app.schemas import GenerateNoteRequest, MediaRequirements, NoteContent, SafetyResult


class MockProvider(AIProviderAdapter):
    def __init__(self, sensitive_keywords: list[str] | None = None):
        self.sensitive_keywords = sensitive_keywords or []

    def classify_safety(self, text: str) -> SafetyResult:
        matches = [word for word in self.sensitive_keywords if word.casefold() in text.casefold()]
        return SafetyResult(
            is_safe=not matches,
            reason="" if not matches else "命中敏感关键词，必须由人工处理",
            matched_keywords=matches,
        )

    def generate_note(self, topic: str | GenerateNoteRequest, style: str = "实用、自然", audience: str = "科技爱好者", **options) -> NoteContent:
        request = coerce_generation_request(topic, style, audience, **options)
        validate_generation_request(request)
        title_prefix = "一个容易被忽略的观点：" if request.controversial_title else ""
        title = f"{title_prefix}{request.topic}的 3 个实用思路"[:100]
        sections = [
            f"最近在整理{request.topic}，这里为{request.audience}总结三个可以直接验证的方向。",
            "第一，从一个明确的小问题开始，先建立可观察的结果，不追求一次做大。",
            "第二，用简单实验记录过程和结果，再根据证据调整方法。",
            "第三，每周复盘一次，把真正有效的步骤沉淀成自己的工作流程。",
        ]
        if request.educational:
            sections.append("理解原理比记住结论更重要，可以分别记录输入、过程、输出和限制条件。")
        if request.growth_oriented:
            sections.append("表达时聚焦读者能带走的具体价值，用真实过程建立长期信任，不使用诱导互动话术。")
        sections.append(f"全文采用{request.style}的表达，并保留适用条件和局限。")
        compact = "".join(sections)
        filler = "实践时建议一次只改变一个变量，并如实记录结果，这样更容易找到真正有效的方法。"
        while len(compact) < request.min_length:
            compact += filler
        body = compact[: request.max_length]
        media_type = "video" if request.publish_kind == "video_upload" else "image"
        if request.publish_kind == "video_upload":
            media_description = (
                f"视频脚本：用 3 个镜头讲清“{request.topic}”；"
                "镜头1提出问题，镜头2展示过程，镜头3总结行动建议。"
                "agent 不负责生成视频文件，用户需要上传本地 mp4/mov。"
            )
        elif request.publish_kind == "image_upload":
            media_description = (
                f"图片计划：围绕“{request.topic}”准备 3-5 张图，包含封面、步骤图、对比图和总结图；"
                "每张图使用清晰标题、简洁构图和真实信息。"
            )
        else:
            media_description = (
                f"小红书文字配图内容：把“{request.topic}”整理成 20-80 字卡片文字，"
                "适合套入备忘录风格模板，避免夸张承诺和诱导互动。"
            )
        note = NoteContent(
            title=title,
            body=body,
            hashtags=[request.topic.replace(" ", "")[:20], "科技", "效率工具"],
            cover_prompt="",
            media_requirements=MediaRequirements(type=media_type, count=1 if request.publish_kind != "image_upload" else 3, description=media_description),
            safety=self.classify_safety(f"{title}\n{body}"),
        )
        note.cover_prompt = self.generate_cover_prompt(note)
        validate_note_content(note, request)
        return note

    def generate_reply(self, message: str, context: str = "") -> str:
        if not self.classify_safety(message).is_safe:
            raise ValueError("Sensitive messages must not use free-form generation")
        return "谢谢你的分享，这个角度很有启发。"

    def generate_cover_prompt(self, note: NoteContent) -> str:
        return f"竖版 3:4 小红书封面，简洁科技感，醒目中文标题：{note.title}，留白充足"

    def chat_text(self, prompt: str) -> str:
        return '{"ok": true}' if '"ok"' in prompt else "mock response"

    def chat_json(self, prompt: str) -> dict:
        return {"ok": True}
