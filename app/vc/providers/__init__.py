"""Cloud image / video providers. `REGISTRY` maps provider id to a stateless provider instance."""
from __future__ import annotations

from ..util import AppError
from .base import Context, Poll, Provider, Request
from .custom import CustomHttp
from .hosted import FalImage, FalVideo, ReplicateImage, ReplicateVideo
from .images import GeminiImage, OpenAIImages, StabilityImage
from .video import GeminiOmni, Kling, MiniMax, Runway

_ORDER: tuple[Provider, ...] = (
    OpenAIImages(),
    GeminiImage(),
    FalImage(),
    ReplicateImage(),
    StabilityImage(),
    CustomHttp("custom-image", "image"),
    Kling(),
    Runway(),
    MiniMax(),
    GeminiOmni(),
    FalVideo(),
    ReplicateVideo(),
    CustomHttp("custom-video", "video"),
)
REGISTRY: dict[str, Provider] = {p.id: p for p in _ORDER}


def get(pid: str) -> Provider:
    provider = REGISTRY.get(pid)
    if provider is None:
        raise AppError(f"找不到雲端供應商：{pid}", 404)
    return provider


def list_all(kind: str | None = None) -> list[Provider]:
    return [p for p in _ORDER if kind is None or p.kind == kind]


__all__ = ["Context", "Poll", "Provider", "REGISTRY", "Request", "get", "list_all"]
