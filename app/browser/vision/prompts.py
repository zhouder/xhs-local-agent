from __future__ import annotations

import json

from app.browser.vision.types import VisionObservation


VISION_SYSTEM_PROMPT = (
    "你是一个受限浏览器页面视觉定位器。只根据截图判断目标控件位置。"
    "必须只输出一个 JSON 对象，不要使用 Markdown，不要输出自然语言。"
    "如果看不到目标或目标不确定，返回 ok=false。"
)


def build_vision_user_prompt(observation: VisionObservation, goal: str, forbidden_click_texts: list[str]) -> str:
    schema = {
        "ok": True,
        "action": {
            "type": "click",
            "target_label": "目标名称",
            "x": 0,
            "y": 0,
            "confidence": 0.0,
            "reason": "为什么这是目标",
            "visible_text": "目标附近可见文字",
        },
        "targets": [
            {
                "label": "目标名称",
                "bbox": {"x": 0, "y": 0, "width": 0, "height": 0},
                "center": {"x": 0, "y": 0},
                "confidence": 0.0,
                "reason": "为什么这是候选",
                "visible_text": "候选可见文字",
            }
        ],
        "refusal_reason": None,
    }
    return json.dumps({
        "task": "定位浏览器页面上的可点击或可输入目标",
        "goal": goal,
        "step": observation.step,
        "url": observation.url,
        "title": observation.title,
        "viewport": {"width": observation.viewport_width, "height": observation.viewport_height},
        "page_text_summary": observation.page_text_summary,
        "forbidden_click_texts": forbidden_click_texts,
        "output_schema_example": schema,
        "rules": [
            "坐标必须是当前截图视口内的页面坐标。",
            "不要选择包含 forbidden_click_texts 的按钮或链接。",
            "如果目标文字在整页大容器中，不要返回大容器，返回具体按钮或输入区中心。",
            "找不到明确目标时返回 ok=false、action=null、targets=[]。",
        ],
    }, ensure_ascii=False)

