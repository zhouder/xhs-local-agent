from __future__ import annotations


PUBLISH_KIND_VIDEO_UPLOAD = "video_upload"
PUBLISH_KIND_IMAGE_UPLOAD = "image_upload"
PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE = "image_text_to_image"

PUBLISH_KINDS = {
    PUBLISH_KIND_VIDEO_UPLOAD,
    PUBLISH_KIND_IMAGE_UPLOAD,
    PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE,
}

PUBLISH_KIND_LABELS = {
    PUBLISH_KIND_VIDEO_UPLOAD: "视频笔记：上传视频",
    PUBLISH_KIND_IMAGE_UPLOAD: "图文笔记：上传自己的图片",
    PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE: "图文笔记：文字生图",
}


def normalize_publish_kind(value: str | None) -> str:
    kind = (value or "").strip()
    if kind in PUBLISH_KINDS:
        return kind
    return PUBLISH_KIND_IMAGE_TEXT_TO_IMAGE


def publish_target_for_kind(kind: str | None) -> str:
    normalized = normalize_publish_kind(kind)
    if normalized == PUBLISH_KIND_VIDEO_UPLOAD:
        return "video"
    return "image"


def publish_kind_label(kind: str | None) -> str:
    return PUBLISH_KIND_LABELS[normalize_publish_kind(kind)]
