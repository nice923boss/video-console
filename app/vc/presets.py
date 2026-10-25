"""Static choice lists for the UI: visual styles, camera moves, shot sizes, frame sizes, transitions."""
from __future__ import annotations

from typing import Any

from .media import KENBURNS_LABELS
from .opfields import TRANSITIONS

# Style prompts are English so they also work with CLIP-based workflows a user imports;
# labels and descriptions are what the UI shows.
STYLES: list[dict[str, str]] = [
    {"id": "cinematic", "label": "電影寫實", "desc": "自然光、淺景深、電影調色",
     "prompt": "cinematic film still, photorealistic, natural light, shallow depth of field, anamorphic lens, "
               "subtle film grain, professional color grading",
     "negative": "cartoon, anime, illustration, 3d render, plastic skin, oversaturated"},
    {"id": "anime", "label": "日系動畫", "desc": "乾淨線稿、賽璐璐上色、細緻背景",
     "prompt": "anime style illustration, clean line art, cel shading, vibrant colors, detailed background art, "
               "soft lighting",
     "negative": "photorealistic, photo, 3d render, realistic skin texture"},
    {"id": "3d", "label": "3D 動畫", "desc": "風格化角色、柔和全域光",
     "prompt": "3d animated film style, stylized characters, soft global illumination, subsurface scattering, "
               "vibrant colors, highly detailed render",
     "negative": "photo, live action, flat 2d, sketch"},
    {"id": "watercolor", "label": "水彩插畫", "desc": "柔和暈染、紙張紋理",
     "prompt": "watercolor painting, soft washes, visible paper texture, gentle bleeding edges, pastel palette, "
               "hand-painted illustration",
     "negative": "photo, 3d render, harsh lines, neon colors"},
    {"id": "ink", "label": "水墨國風", "desc": "筆觸流動、留白、宣紙質感",
     "prompt": "traditional Chinese ink wash painting, flowing brush strokes, rice paper texture, minimal color, "
               "elegant negative space",
     "negative": "photo, 3d render, neon colors, cluttered composition"},
    {"id": "pixel", "label": "像素風", "desc": "16 位元復古遊戲畫面",
     "prompt": "pixel art, 16-bit retro game style, limited color palette, crisp pixels, dithering",
     "negative": "blurry, photo, smooth gradients, 3d render"},
    {"id": "clay", "label": "黏土動畫", "desc": "手作黏土、微縮場景",
     "prompt": "claymation stop-motion style, handcrafted clay figures, fingerprint texture, miniature set, "
               "soft studio lighting",
     "negative": "photo, realistic skin, flat 2d, line art"},
    {"id": "cyberpunk", "label": "賽博龐克", "desc": "霓虹、雨夜、高反差",
     "prompt": "cyberpunk city atmosphere, neon lights, rain-soaked streets, reflections, high contrast, "
               "teal and magenta color palette, cinematic",
     "negative": "daylight countryside, washed out, low contrast"},
    {"id": "documentary", "label": "紀錄片", "desc": "手持感、自然光、真實瞬間",
     "prompt": "documentary photography, handheld camera feel, natural available light, authentic candid moment, "
               "realistic detail",
     "negative": "staged, glossy, cartoon, oversaturated"},
    {"id": "noir", "label": "黑白電影", "desc": "明暗強烈、深邃陰影",
     "prompt": "black and white film noir, high contrast chiaroscuro lighting, deep shadows, dramatic composition, "
               "film grain",
     "negative": "color, saturated colors, flat lighting"},
    {"id": "vintage", "label": "復古底片", "desc": "35mm 底片、暖色褪色、漏光",
     "prompt": "vintage 35mm film photograph, warm faded colors, light leaks, soft grain, nostalgic mood",
     "negative": "digital sharpness, hdr, oversaturated, modern look"},
    {"id": "product", "label": "產品廣告", "desc": "棚拍光、乾淨背景、高級質感",
     "prompt": "premium product commercial, studio lighting, clean background, crisp reflections, "
               "high-end advertising photography, sharp focus",
     "negative": "clutter, messy background, low quality, blurry"},
]

# Camera moves: `text` goes into the shot's camera field (it is part of the video prompt),
# `kenburns` is the matching still-image move for the ffmpeg fallback.
CAMERA_MOVES: list[dict[str, str]] = [
    {"id": "static", "label": "固定", "text": "固定鏡頭，畫面穩定", "en": "static camera, locked-off shot",
     "kenburns": "static"},
    {"id": "push_in", "label": "推進", "text": "鏡頭緩慢推進", "en": "slow dolly in", "kenburns": "zoom_in"},
    {"id": "pull_out", "label": "拉遠", "text": "鏡頭緩慢拉遠", "en": "slow dolly out", "kenburns": "zoom_out"},
    {"id": "pan_left", "label": "左搖", "text": "鏡頭向左平移", "en": "camera pans left", "kenburns": "pan_left"},
    {"id": "pan_right", "label": "右搖", "text": "鏡頭向右平移", "en": "camera pans right", "kenburns": "pan_right"},
    {"id": "tilt_up", "label": "上搖", "text": "鏡頭向上搖", "en": "camera tilts up", "kenburns": "pan_up"},
    {"id": "tilt_down", "label": "下搖", "text": "鏡頭向下搖", "en": "camera tilts down", "kenburns": "pan_down"},
    {"id": "orbit", "label": "環繞", "text": "鏡頭環繞主體", "en": "camera orbits around the subject",
     "kenburns": "zoom_in_right"},
    {"id": "follow", "label": "跟拍", "text": "鏡頭跟隨主體移動", "en": "tracking shot following the subject",
     "kenburns": "pan_right"},
    {"id": "handheld", "label": "手持", "text": "手持鏡頭，輕微晃動", "en": "handheld camera, subtle shake",
     "kenburns": "zoom_in"},
    {"id": "crane_up", "label": "升起", "text": "鏡頭緩慢升起", "en": "crane shot rising up", "kenburns": "pan_up"},
    {"id": "aerial", "label": "空拍", "text": "空拍俯瞰，緩慢前進", "en": "aerial drone shot, slowly flying forward",
     "kenburns": "zoom_in"},
]

SHOT_SIZES: list[dict[str, str]] = [
    {"id": "ews", "label": "大遠景", "text": "大遠景", "en": "extreme wide shot"},
    {"id": "ws", "label": "遠景", "text": "遠景", "en": "wide shot"},
    {"id": "fs", "label": "全景", "text": "全景", "en": "full shot"},
    {"id": "ms", "label": "中景", "text": "中景", "en": "medium shot"},
    {"id": "mcu", "label": "近景", "text": "近景", "en": "medium close-up"},
    {"id": "cu", "label": "特寫", "text": "特寫", "en": "close-up"},
    {"id": "ecu", "label": "大特寫", "text": "大特寫", "en": "extreme close-up"},
]

# Frame sizes are multiples of 8 (1080 is not a multiple of 16); the first size of each aspect is the default
# picked with it. Generation never goes above a 720 short side (runners.GEN_SHORT_SIDE); the export scales up.
ASPECTS: list[dict[str, Any]] = [
    {"id": "16:9", "label": "16:9 橫式", "sizes": [[1920, 1080], [1280, 720], [1024, 576], [768, 448]]},
    {"id": "9:16", "label": "9:16 直式", "sizes": [[448, 768], [576, 1024], [720, 1280], [1080, 1920]]},
    {"id": "1:1", "label": "1:1 方形", "sizes": [[640, 640], [768, 768], [1024, 1024]]},
    {"id": "4:3", "label": "4:3", "sizes": [[768, 576], [1024, 768]]},
    {"id": "3:4", "label": "3:4", "sizes": [[576, 768], [768, 1024]]},
    {"id": "2.39:1", "label": "2.39:1 寬銀幕", "sizes": [[912, 384], [1216, 512]]},
]

TRANSITION_LABELS = {
    "cut": "直接切換",
    "crossfade": "交叉溶接",
    "fade_black": "黑場轉場",
    "fade_white": "白場轉場",
    "wipe_left": "向左擦除",
    "slide_left": "向左滑入",
}


def all_presets() -> dict[str, Any]:
    return {
        "styles": STYLES,
        "cameraMoves": CAMERA_MOVES,
        "shotSizes": SHOT_SIZES,
        "aspects": ASPECTS,
        "kenburns": [{"id": k, "label": v} for k, v in KENBURNS_LABELS.items()],
        "transitions": [{"id": t, "label": TRANSITION_LABELS.get(t, t)} for t in TRANSITIONS],
    }
