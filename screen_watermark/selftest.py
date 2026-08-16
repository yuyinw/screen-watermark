from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .core import WatermarkConfig, apply_adaptive_overlay_to_image, apply_overlay_to_image, decode_payload_from_image


def _background(width: int, height: int) -> Image.Image:
    x = np.linspace(0, 1, width, dtype=np.float32)
    y = np.linspace(0, 1, height, dtype=np.float32)
    xx, yy = np.meshgrid(x, y)
    r = 40 + 190 * xx
    g = 65 + 140 * yy
    b = 130 + 80 * (1 - xx * yy)
    arr = np.dstack([r, g, b]).clip(0, 255).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    draw = ImageDraw.Draw(img)
    for i in range(24):
        x0 = 30 + (i * 83) % (width - 180)
        y0 = 40 + (i * 57) % (height - 120)
        color = (25 + i * 7 % 180, 40 + i * 17 % 180, 60 + i * 29 % 160)
        draw.rectangle([x0, y0, x0 + 150, y0 + 76], fill=color)
        draw.line([x0, y0 + 18, x0 + 150, y0 + 18], fill=(230, 230, 230), width=1)
    return img


def _check(
    name: str,
    image: Image.Image,
    expected: str,
    config: WatermarkConfig,
    tile_px: int = 256,
    offset_step: int | None = None,
) -> None:
    result = decode_payload_from_image(image, config=config, tile_px=tile_px, offset_step=offset_step)
    ok = result.payload_hex == expected.lower()
    status = "OK" if ok else "FAIL"
    print(
        f"{name}: {status} payload={result.payload_hex} "
        f"bit_conf={result.bit_confidence:.3f} sync_conf={result.sync_confidence:.3f} "
        f"tiles={result.tile_count} offset={result.offset_x},{result.offset_y}"
    )
    if not ok:
        raise SystemExit(1)


def _perspective_camera_sim(image: Image.Image) -> Image.Image:
    import cv2

    arr = np.asarray(image.convert("RGB"))
    height, width = arr.shape[:2]
    canvas_w, canvas_h = 2200, 1500
    src = np.asarray([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    dst = np.asarray([[180, 110], [2020, 160], [1970, 1370], [230, 1310]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(arr, matrix, (canvas_w, canvas_h), borderValue=(32, 32, 32))
    warped = cv2.GaussianBlur(warped, (0, 0), 0.7)
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 88]
    _, encoded = cv2.imencode(".jpg", cv2.cvtColor(warped, cv2.COLOR_RGB2BGR), encode_param)
    decoded = cv2.cvtColor(cv2.imdecode(encoded, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    return Image.fromarray(decoded)


def main() -> None:
    out_dir = Path("artifacts")
    out_dir.mkdir(exist_ok=True)
    payload = "1a2b3c4d"
    config = WatermarkConfig(tile_size=256, grid_size=32)

    base = _background(1920, 1080)
    watermarked = apply_overlay_to_image(base, payload, opacity=0.075, config=config)
    watermarked.save(out_dir / "selftest_watermarked.png")
    _check("full", watermarked, payload, config)

    crop = watermarked.crop((317, 211, 317 + 960, 211 + 640))
    crop.save(out_dir / "selftest_crop.png")
    _check("crop", crop, payload, config)

    small = watermarked.resize((1280, 720), Image.Resampling.BICUBIC)
    small = small.filter(ImageFilter.GaussianBlur(radius=0.45))
    small.save(out_dir / "selftest_scaled_blur.png")
    _check("scaled_blur", small, payload, config, tile_px=171)

    camera_cfg = WatermarkConfig(tile_size=256, grid_size=32, channel="chroma", smooth_radius=0.5, sync_strength=0.55)
    adaptive = apply_adaptive_overlay_to_image(_background(1920, 1200), payload, opacity=0.055, amplitude=8.0, config=camera_cfg)
    adaptive.save(out_dir / "selftest_adaptive_chroma.png")
    _check("adaptive_chroma", adaptive, payload, camera_cfg, tile_px=256)

    fsk_cfg = WatermarkConfig(
        tile_size=256,
        grid_size=64,
        channel="luma",
        algorithm="fsk",
        smooth_radius=0.4,
        sync_strength=0.8,
    )
    fsk = apply_adaptive_overlay_to_image(_background(1920, 1200), payload, opacity=0.055, amplitude=4.0, config=fsk_cfg)
    fsk.save(out_dir / "selftest_fsk.png")
    _check("fsk", fsk, payload, fsk_cfg, tile_px=256)

    fsk_crop = fsk.crop((310, 205, 310 + 1000, 205 + 700))
    fsk_crop.save(out_dir / "selftest_fsk_crop.png")
    _check("fsk_crop", fsk_crop, payload, fsk_cfg, tile_px=256, offset_step=8)

    fsk_small = fsk.resize((1280, 800), Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(radius=0.55))
    fsk_small.save(out_dir / "selftest_fsk_scaled_blur.png")
    _check("fsk_scaled_blur", fsk_small, payload, fsk_cfg, tile_px=171)

    print(f"artifacts={out_dir.resolve()}")


if __name__ == "__main__":
    main()
