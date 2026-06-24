from __future__ import annotations

from urllib.parse import urlparse

from app.browser.vision.types import VisionAction, VisionObservation
from app.config import Settings


class VisionSafetyError(PermissionError):
    pass


def vision_allowed_domains(settings: Settings) -> list[str]:
    return list(settings.browser.get("visual_mode_allowed_domains", ["creator.xiaohongshu.com"]))


def vision_forbidden_click_texts(settings: Settings) -> list[str]:
    return list(settings.browser.get("visual_mode_forbidden_click_texts", ["发布", "立即发布", "确认发布", "支付", "授权", "同意"]))


def vision_confidence_threshold(settings: Settings) -> float:
    return float(settings.browser.get("visual_mode_confidence_threshold", 0.65))


def assert_allowed_url(url: str, settings: Settings) -> str:
    host = urlparse(url or "").netloc.casefold()
    allowed = [domain.casefold() for domain in vision_allowed_domains(settings)]
    if not host or not any(host == domain or host.endswith("." + domain) for domain in allowed):
        raise VisionSafetyError(f"视觉动作被安全策略阻止：当前域名不在允许列表。current_host={host or '-'}")
    return host


def validate_vision_action(observation: VisionObservation, action: VisionAction, settings: Settings, *, mode: str, final_confirm: bool = False) -> None:
    assert_allowed_url(observation.url, settings)
    if action.x is None or action.y is None:
        raise VisionSafetyError("视觉动作被安全策略阻止：缺少点击坐标。")
    if not (0 <= action.x <= observation.viewport_width and 0 <= action.y <= observation.viewport_height):
        raise VisionSafetyError("视觉动作被安全策略阻止：坐标超出浏览器视口。")
    threshold = vision_confidence_threshold(settings)
    if action.confidence < threshold:
        raise VisionSafetyError(f"视觉模式找到了【{action.target_label}】，但置信度低于阈值。confidence={action.confidence:.2f}; threshold={threshold:.2f}")
    combined_text = f"{action.target_label} {action.visible_text}".casefold()
    for forbidden in vision_forbidden_click_texts(settings):
        if forbidden and forbidden.casefold() in combined_text:
            if final_confirm:
                continue
            raise VisionSafetyError(f"视觉动作被安全策略阻止：目标文字包含【{forbidden}】。")
    if mode == "fill_only" and any(token in combined_text for token in ("发布", "立即发布", "确认发布")):
        raise VisionSafetyError("视觉动作被安全策略阻止：fill_only 不能点击发布。")

