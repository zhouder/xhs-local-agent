from __future__ import annotations

import re

from app.schemas import GenerateNoteRequest, NoteContent


class UnsafeAIContentError(ValueError):
    pass


SENSITIVE_ADVICE_PATTERNS = {
    "医疗建议": ("诊断", "用药", "药量", "治疗方案", "医疗建议"),
    "法律建议": ("法律建议", "诉讼策略", "规避法律", "保证胜诉"),
    "金融建议": ("投资建议", "荐股", "稳赚", "保本收益", "保证收益"),
    "政治敏感": ("政治动员", "政治立场", "政治建议", "选举建议"),
}

PROHIBITED_MARKETING_PATTERNS = (
    "关注我", "求关注", "记得关注", "点点关注", "点赞关注", "点赞收藏", "私信领取", "评论区扣", "评论区见", "互关", "刷赞",
    "百分百有效", "绝对有效", "保证成功", "一夜暴富", "月入百万", "零风险",
)


def validate_generation_request(request: GenerateNoteRequest) -> None:
    text = f"{request.topic} {request.style} {request.audience}".casefold()
    blocked = ("医疗建议", "诊断建议", "用药建议", "法律建议", "投资建议", "政治动员", "荐股")
    matches = [item for item in blocked if item.casefold() in text]
    if matches:
        raise UnsafeAIContentError(f"请求涉及不允许生成的敏感建议：{', '.join(matches)}")


def validate_note_content(note: NoteContent, request: GenerateNoteRequest) -> None:
    text = f"{note.title}\n{note.body}\n{' '.join(note.hashtags)}"
    compact_length = len(re.sub(r"\s+", "", note.body))
    if not request.min_length <= compact_length <= request.max_length:
        raise ValueError(f"正文长度 {compact_length} 不在 {request.min_length}-{request.max_length} 范围内")
    violations: list[str] = []
    for category, patterns in SENSITIVE_ADVICE_PATTERNS.items():
        if any(pattern in text for pattern in patterns):
            violations.append(category)
    if any(pattern in text for pattern in PROHIBITED_MARKETING_PATTERNS):
        violations.append("违规营销或互动话术")
    if violations:
        raise UnsafeAIContentError(f"生成内容未通过本地安全检查：{', '.join(sorted(set(violations)))}")
    if not note.safety.is_safe:
        raise UnsafeAIContentError(f"模型将内容标记为不安全：{note.safety.reason or '未提供原因'}")
