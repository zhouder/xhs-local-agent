from __future__ import annotations

import re


DEFAULT_HASHTAGS = ["AI", "科技", "效率工具", "学习方法", "职场提升", "内容创作"]


def split_hashtags(text: str) -> list[str]:
    parts = re.split(r"[\s,，、;；]+", text or "")
    return normalize_hashtags(parts)


def extract_hashtags_from_body(body: str) -> list[str]:
    return normalize_hashtags(re.findall(r"#([\w\u4e00-\u9fff-]{1,30})", body or ""))


def normalize_hashtags(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items or []:
        tag = str(item).strip().lstrip("#").strip()
        tag = re.sub(r"[^\w\u4e00-\u9fff-]", "", tag)
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag[:30])
        if len(result) >= 8:
            break
    return result


def ensure_hashtags(title: str, body: str, hashtags=None) -> list[str]:
    result = normalize_hashtags(hashtags or [])
    if not result:
        result = extract_hashtags_from_body(body)
    if not result:
        base_text = f"{title} {body}".casefold()
        candidates = []
        if "ai" in base_text or "人工智能" in base_text:
            candidates.append("AI")
        if "编程" in base_text or "代码" in base_text:
            candidates.append("编程")
        if "工具" in base_text or "效率" in base_text:
            candidates.append("效率工具")
        candidates.extend(DEFAULT_HASHTAGS)
        result = normalize_hashtags(candidates)
    while len(result) < 3:
        for fallback in DEFAULT_HASHTAGS:
            if fallback not in result:
                result.append(fallback)
                break
    return result[:8]
