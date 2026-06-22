from __future__ import annotations

from app.security import redact_secrets


def friendly_connection_error(error: Exception) -> str:
    text = str(redact_secrets(str(error)))
    folded = text.casefold()
    if "请求 url：" in folded and "content-type：" in folded:
        return text[:1000]
    if "http 401" in folded or "unauthorized" in folded:
        return "401 Unauthorized，请检查 API Key。"
    if "http 403" in folded or "forbidden" in folded:
        return "403 Forbidden，请检查 API Key 权限。"
    if "http 404" in folded or "not found" in folded:
        return "404 Not Found，请检查 Base URL 或 API 格式。"
    if "http 400" in folded or "bad request" in folded:
        return "400 Bad Request，请检查模型 ID 和请求参数。"
    if "timed out" in folded or "timeout" in folded:
        return "请求超时，请检查网络或 Base URL。"
    if "invalid structured" in folded or "not valid json" in folded or "invalid response envelope" in folded or "not valid json" in folded:
        return "接口返回的不是可解析 JSON。"
    if "model" in folded and ("not" in folded or "不存在" in text):
        return "模型不存在，请检查模型 ID。"
    if "network error" in folded:
        return "网络请求失败，请检查网络或 Base URL。"
    return text[:300] or "未知错误"
