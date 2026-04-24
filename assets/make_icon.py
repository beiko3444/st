"""SmartInventory 앱 아이콘 생성기.

컨셉: 여러 채널 상품(네이버/쿠팡) → 하나의 마스터 상품으로 통합.
3단 쌓인 박스로 "집계/통합" 은유. 상단 큰 박스(마스터) + 하단 두 박스(채널).

출력:
- assets/icon.png          1024×1024 PNG (마스터 소스)
- assets/icon.iconset/     macOS iconutil 입력 (여러 사이즈 @1x/@2x)
- assets/icon.icns         최종 macOS 아이콘 (iconutil 호출)
- assets/icon.ico          Windows 용
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parent
OUT_PNG = ROOT / "icon.png"
OUT_ICONSET = ROOT / "icon.iconset"
OUT_ICNS = ROOT / "icon.icns"
OUT_ICO = ROOT / "icon.ico"


# ---- 색상 팔레트 ------------------------------------------------------------

BG_TOP = (15, 23, 42)         # slate-900
BG_BOTTOM = (14, 116, 144)    # cyan-700
NAVER_GREEN = (3, 199, 90)    # smartstore 시그니처
COUPANG_RED = (255, 58, 58)   # 쿠팡 시그니처
MASTER_GOLD = (251, 191, 36)  # amber-400
BOX_EDGE = (15, 23, 42)       # slate-900
HIGHLIGHT = (255, 255, 255, 70)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _rounded_rect_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _gradient(size: tuple[int, int]) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, BG_TOP)
    pixels = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        color = _lerp(BG_TOP, BG_BOTTOM, t)
        for x in range(w):
            pixels[x, y] = color
    return img


def _draw_box(
    canvas: Image.Image,
    cx: float,
    cy: float,
    width: float,
    depth: float,
    height: float,
    face_color: tuple[int, int, int],
    side_color: tuple[int, int, int] | None = None,
    top_color: tuple[int, int, int] | None = None,
) -> None:
    """아이소메트릭 박스를 그린다.

    좌표는 캔버스 중심 기준 2D 투영. iso_ratio=0.5 (y축 기울기).
    front face / top / side 3면 렌더링.
    """
    side_color = side_color or _lerp(face_color, (0, 0, 0), 0.35)
    top_color = top_color or _lerp(face_color, (255, 255, 255), 0.18)

    half_w = width / 2
    iso = 0.5  # depth 투영 비율

    # 3D 꼭짓점 → 2D
    # 전면 사각형 4꼭짓점 (bottom-left, bottom-right, top-right, top-left)
    bl = (cx - half_w, cy + height / 2)
    br = (cx + half_w, cy + height / 2)
    tr = (cx + half_w, cy - height / 2)
    tl = (cx - half_w, cy - height / 2)

    # depth 벡터 (오른쪽 위로)
    dx = depth * math.cos(math.radians(30))
    dy = -depth * math.sin(math.radians(30))

    bl_back = (bl[0] + dx, bl[1] + dy)
    br_back = (br[0] + dx, br[1] + dy)
    tr_back = (tr[0] + dx, tr[1] + dy)
    tl_back = (tl[0] + dx, tl[1] + dy)

    draw = ImageDraw.Draw(canvas)

    # side (오른쪽 면)
    draw.polygon([br, tr, tr_back, br_back], fill=side_color, outline=BOX_EDGE)
    # top (위쪽 면)
    draw.polygon([tl, tr, tr_back, tl_back], fill=top_color, outline=BOX_EDGE)
    # front
    draw.polygon([bl, br, tr, tl], fill=face_color, outline=BOX_EDGE)

    # 상자 테이프(가로 줄) — 디테일 강조
    tape_color = _lerp(face_color, (0, 0, 0), 0.2)
    tape_y = cy - height / 2 + height * 0.08
    draw.rectangle(
        (bl[0] + width * 0.04, tape_y, br[0] - width * 0.04, tape_y + max(3, height * 0.04)),
        fill=tape_color,
    )


def render_icon(size: int) -> Image.Image:
    """지정 사이즈의 RGBA 아이콘 렌더."""
    # 고해상도로 그리고 마지막에 축소 (안티앨리어싱)
    scale = 2 if size >= 256 else 4
    w = size * scale
    canvas = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    # 배경 (둥근 사각형 + 그라데이션)
    radius = int(w * 0.22)
    bg = _gradient((w, w))
    bg_rgba = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    mask = _rounded_rect_mask((w, w), radius)
    bg_rgba.paste(bg, (0, 0), mask)

    # 상단 하이라이트 (유리같은 느낌)
    highlight = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.ellipse(
        (-w * 0.1, -w * 0.6, w * 1.1, w * 0.55),
        fill=(255, 255, 255, 30),
    )
    highlight.putalpha(ImageOps_mask_alpha(highlight, mask))
    bg_rgba = Image.alpha_composite(bg_rgba, highlight)

    # 박스 배치 — 아이콘 중앙 기준
    cx = w / 2
    cy = w / 2 + w * 0.04
    base_w = w * 0.36
    base_h = w * 0.32
    base_d = w * 0.18

    # 하단 좌: 네이버 (뒤쪽)
    _draw_box(
        bg_rgba,
        cx - base_w * 0.45,
        cy + base_h * 0.4,
        base_w * 0.9,
        base_d,
        base_h * 0.78,
        NAVER_GREEN,
    )
    # 하단 우: 쿠팡
    _draw_box(
        bg_rgba,
        cx + base_w * 0.45,
        cy + base_h * 0.4,
        base_w * 0.9,
        base_d,
        base_h * 0.78,
        COUPANG_RED,
    )
    # 상단: 마스터 (큰 골드 박스)
    _draw_box(
        bg_rgba,
        cx,
        cy - base_h * 0.45,
        base_w * 1.12,
        base_d * 1.15,
        base_h * 0.95,
        MASTER_GOLD,
    )

    # 마스터 박스에 ★ 별 그림 — "대표 상품" 상징
    star_layer = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    sd = ImageDraw.Draw(star_layer)
    star_r = base_w * 0.18
    sx = cx
    sy = cy - base_h * 0.45
    points = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = star_r if i % 2 == 0 else star_r * 0.45
        points.append((sx + r * math.cos(angle), sy + r * math.sin(angle)))
    sd.polygon(points, fill=(255, 255, 255, 240), outline=(15, 23, 42, 255))
    bg_rgba = Image.alpha_composite(bg_rgba, star_layer)

    # 소프트 드롭섀도우 (박스 아래)
    shadow = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    sdr = ImageDraw.Draw(shadow)
    sdr.ellipse(
        (cx - base_w * 1.1, cy + base_h * 0.95, cx + base_w * 1.1, cy + base_h * 1.15),
        fill=(0, 0, 0, 110),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(2, w // 120)))
    # mask 를 씌워 배경 밖으로 안 나가게
    shadow_masked = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    shadow_masked.paste(shadow, (0, 0), mask)
    final = Image.alpha_composite(shadow_masked, bg_rgba)

    # 최종 축소
    final = final.resize((size, size), Image.LANCZOS)
    return final


def ImageOps_mask_alpha(image: Image.Image, mask: Image.Image) -> Image.Image:
    """image 의 alpha 채널에 mask(L) 를 곱해 반환."""
    _r, _g, _b, a = image.split()
    new_a = Image.new("L", image.size, 0)
    new_a.paste(a, (0, 0), mask)
    return new_a


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    master = render_icon(1024)
    master.save(OUT_PNG, "PNG")
    print(f"wrote {OUT_PNG}")

    # macOS iconset 용 여러 사이즈
    if OUT_ICONSET.exists():
        for p in OUT_ICONSET.iterdir():
            p.unlink()
    OUT_ICONSET.mkdir(parents=True, exist_ok=True)
    iconset_specs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, name in iconset_specs:
        img = render_icon(size)
        img.save(OUT_ICONSET / name, "PNG")
    print(f"wrote {OUT_ICONSET}")

    # icns 생성 (macOS only, iconutil 필요)
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["iconutil", "-c", "icns", str(OUT_ICONSET), "-o", str(OUT_ICNS)],
                check=True,
            )
            print(f"wrote {OUT_ICNS}")
        except FileNotFoundError:
            print("iconutil not found; skip .icns")

    # Windows .ico (PIL 로 다중 사이즈 내장)
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(OUT_ICO, format="ICO", sizes=ico_sizes)
    print(f"wrote {OUT_ICO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
