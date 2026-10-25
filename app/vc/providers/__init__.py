"""Cloud image / video providers. `REGISTRY` maps provider id to a stateless provider instance."""
from __future__ import annotations

from ..util import AppError
from .base import Context, Poll, Provider, Request, number
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
    GeminiOmni(),
    Runway(),
    FalVideo(),
    ReplicateVideo(),
    Kling(),
    MiniMax(),
    CustomHttp("custom-video", "video"),
)
REGISTRY: dict[str, Provider] = {p.id: p for p in _ORDER}

# Every provider takes an optional unit price, used only for the spend estimate (CL-01, vc/costs.py).
for _p in _ORDER:
    _p.fields = (*_p.fields, number(
        "price", f"{'每秒' if _p.kind == 'video' else '每張'}費用（美元，選填）", 0.0, 0.0, 100.0, 0.001, digits=3,
        help="填了才會在送出前預估費用並記進本月累計，0 表示不估算；實際金額以供應商帳單為準"))


def get(pid: str) -> Provider:
    provider = REGISTRY.get(pid)
    if provider is None:
        raise AppError(f"找不到雲端供應商：{pid}", 404)
    return provider


def list_all(kind: str | None = None) -> list[Provider]:
    return [p for p in _ORDER if kind is None or p.kind == kind]


__all__ = ["Context", "Poll", "Provider", "REGISTRY", "Request", "get", "list_all"]
