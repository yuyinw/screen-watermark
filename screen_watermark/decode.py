from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from .core import WatermarkConfig, decode_payload_from_image


def _dedupe_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    output: list[int] = []
    for value in values:
        if value not in seen and value > 0:
            seen.add(value)
            output.append(value)
    return output


def _parse_tile_range(value: str) -> list[int]:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("tile range must be start:end:step, for example 220:340:4")
    start, end, step = (int(part) for part in parts)
    if step <= 0 or end < start:
        raise argparse.ArgumentTypeError("tile range must have end >= start and step > 0")
    return list(range(start, end + 1, step))


def _parse_size(value: str) -> tuple[int, int]:
    width, height = value.lower().split("x", 1)
    return int(width), int(height)


def _parse_corners(value: str) -> np.ndarray:
    points: list[list[float]] = []
    for point in value.split():
        x, y = point.split(",", 1)
        points.append([float(x), float(y)])
    if len(points) != 4:
        raise argparse.ArgumentTypeError("corners must contain four x,y pairs")
    return np.asarray(points, dtype=np.float32)


def _distance(point_a: np.ndarray, point_b: np.ndarray) -> float:
    return float(np.linalg.norm(point_a.astype(np.float32) - point_b.astype(np.float32)))


def _infer_warp_size(corners: np.ndarray, max_side: int = 2200) -> tuple[int, int]:
    rect = _order_points(corners.astype(np.float32))
    top = _distance(rect[0], rect[1])
    bottom = _distance(rect[3], rect[2])
    left = _distance(rect[0], rect[3])
    right = _distance(rect[1], rect[2])
    width = max(1.0, (top + bottom) * 0.5)
    height = max(1.0, (left + right) * 0.5)
    scale = min(1.0, max_side / max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _rectify_with_corners(image: Image.Image, corners: np.ndarray, size: tuple[int, int]) -> Image.Image:
    import cv2

    width, height = size
    src = corners.astype(np.float32)
    dst = np.asarray([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    bgr = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    warped = cv2.warpPerspective(bgr, matrix, (width, height))
    rgb = cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _auto_tile_size(
    image_size: tuple[int, int],
    nominal_tile_size: int,
    nominal_screen_size: tuple[int, int] = (1920, 1200),
) -> int:
    width, height = image_size
    nominal_w, nominal_h = nominal_screen_size
    scale = min(width / nominal_w, height / nominal_h)
    center = int(round(nominal_tile_size * scale))
    if center <= 0:
        center = nominal_tile_size
    return center


def _auto_tile_sizes(
    image_size: tuple[int, int],
    nominal_tile_size: int,
    nominal_screen_size: tuple[int, int] = (1920, 1200),
    quick: bool = True,
) -> list[int]:
    center = _auto_tile_size(image_size, nominal_tile_size, nominal_screen_size)
    if quick:
        values = [center]
    else:
        step = max(4, int(round(center * 0.04)))
        spread = max(24, int(round(center * 0.18)))
        values = list(range(max(24, center - spread), center + spread + 1, step))
    values.extend([center, nominal_tile_size])
    return _dedupe_ints(sorted(values))


def _order_points(points: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).reshape(-1)
    rect[0] = points[np.argmin(sums)]
    rect[2] = points[np.argmax(sums)]
    rect[1] = points[np.argmin(diffs)]
    rect[3] = points[np.argmax(diffs)]
    return rect


def _auto_screen_corners(image: Image.Image) -> np.ndarray | None:
    import cv2

    max_side = 1200
    scale = min(1.0, max_side / max(image.width, image.height))
    work = image
    if scale < 1.0:
        work = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.BICUBIC)

    bgr = cv2.cvtColor(np.asarray(work.convert("RGB")), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    candidates: list[tuple[float, np.ndarray]] = []
    image_area = work.width * work.height
    for low, high in [(25, 80), (40, 120), (60, 160)]:
        edges = cv2.Canny(gray, low, high)
        for kernel_size in (5, 9, 15):
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
            closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
            contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                perimeter = cv2.arcLength(contour, True)
                if perimeter <= 0:
                    continue
                approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
                if len(approx) != 4:
                    continue
                points = _order_points(approx.reshape(4, 2).astype(np.float32))
                area = abs(cv2.contourArea(points))
                if not (image_area * 0.12 <= area <= image_area * 0.92):
                    continue
                x, y, w, h = cv2.boundingRect(points.astype(np.int32))
                if x <= 2 or y <= 2 or x + w >= work.width - 2 or y + h >= work.height - 2:
                    continue
                ratio = w / max(h, 1)
                if not (1.25 <= ratio <= 2.25):
                    continue
                # Prefer large display-like quadrilaterals, but avoid very skinny false positives.
                score = area * (1.0 - min(abs(ratio - 1.6), 0.8) * 0.2)
                candidates.append((score, points))

    if candidates:
        points = max(candidates, key=lambda item: item[0])[1]
        if scale < 1.0:
            points = points / scale
        return points.astype(np.float32)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decode a tiled spread-spectrum watermark.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--tile-px", type=int, default=256, help="Tile size in the image after rectification.")
    parser.add_argument("--tile-range", type=_parse_tile_range, help="Scan tile sizes, for example 220:340:4.")
    parser.add_argument("--auto-tile", action="store_true", help="Infer and scan tile size after rectification.")
    parser.add_argument("--full-tile-scan", action="store_true", help="Use a wider but slower auto tile-size scan.")
    parser.add_argument("--tile-size", type=int, default=256, help="Original tile size used by the encoder.")
    parser.add_argument("--grid-size", type=int, default=32)
    parser.add_argument("--secret", default="desktop-watermark-v1")
    parser.add_argument("--channel", choices=["luma", "chroma"], default="luma")
    parser.add_argument("--algorithm", choices=["pn", "fsk"], default="pn")
    parser.add_argument("--offset-step", type=int)
    parser.add_argument("--max-tiles-per-offset", type=int, default=80)
    parser.add_argument("--keep-tiles", type=int, default=48)
    parser.add_argument("--no-dft-sync", action="store_true", help="Disable DFT phase-correlation offset candidates.")
    parser.add_argument("--corners", type=_parse_corners, help="Four screen corners: 'x,y x,y x,y x,y'.")
    parser.add_argument("--auto-screen", action="store_true", help="Try to find the screen rectangle automatically.")
    parser.add_argument("--allow-unrectified", action="store_true", help="Decode the original image if --auto-screen fails.")
    parser.add_argument("--warp-size", type=_parse_size, help="Rectified screen size, for example 1920x1200. Optional when corners are known.")
    parser.add_argument("--max-warp-side", type=int, default=2200, help="Max side for inferred rectified image size.")
    parser.add_argument("--rectified-out", type=Path)
    parser.add_argument("--min-pattern-confidence", type=float, default=0.22)
    parser.add_argument("--max-corrected-codewords", type=int, default=5)
    parser.add_argument("--camera-mode", action="store_true", help="Preset for photos produced by overlay --camera-mode.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.camera_mode:
        args.algorithm = "fsk"
        args.channel = "luma"
        args.tile_size = 256
        args.grid_size = 64
        args.tile_px = 256
        args.min_pattern_confidence = max(args.min_pattern_confidence, 0.18)
        args.max_corrected_codewords = min(args.max_corrected_codewords, 8)
        args.auto_tile = True
        if args.offset_step is None:
            args.offset_step = 32
        args.max_tiles_per_offset = min(args.max_tiles_per_offset, 40)
        args.keep_tiles = min(args.keep_tiles, 28)

    image = Image.open(args.image).convert("RGB")

    corners = args.corners
    if args.auto_screen and corners is None:
        corners = _auto_screen_corners(image)
        if corners is None:
            print("auto_screen=not_found_using_original_image")
            print("hint=run: python -m screen_watermark.pick_corners <image>")
            if not args.allow_unrectified:
                raise SystemExit(2)

    if corners is not None:
        inferred_warp = args.warp_size is None
        warp_size = args.warp_size or _infer_warp_size(corners, args.max_warp_side)
        print(f"warp_size={warp_size[0]}x{warp_size[1]}")
        if inferred_warp:
            print("warp_size_source=inferred_from_corners")
        image = _rectify_with_corners(image, corners, warp_size)
        if args.rectified_out:
            args.rectified_out.parent.mkdir(parents=True, exist_ok=True)
            image.save(args.rectified_out)

    config = WatermarkConfig(
        tile_size=args.tile_size,
        grid_size=args.grid_size,
        secret=args.secret,
        channel=args.channel,
        algorithm=args.algorithm,
    )
    if args.tile_range:
        tile_sizes = args.tile_range
    elif args.auto_tile:
        tile_sizes = _auto_tile_sizes(image.size, args.tile_size, quick=not args.full_tile_scan)
    else:
        tile_sizes = [args.tile_px]
    results = []
    for tile_px in tile_sizes:
        decoded = decode_payload_from_image(
            image,
            config=config,
            tile_px=tile_px,
            offset_step=args.offset_step,
            max_tiles_per_offset=args.max_tiles_per_offset,
            keep_tiles=args.keep_tiles,
            use_dft_sync=not args.no_dft_sync,
        )
        results.append((tile_px, decoded))
    chosen_tile_px, result = max(
        results,
        key=lambda item: (
            item[1].pattern_confidence,
            -item[1].corrected_codewords,
            item[1].sync_confidence,
            item[1].bit_confidence,
        ),
    )
    status = "ok"
    if (
        result.pattern_confidence < args.min_pattern_confidence
        or result.corrected_codewords > args.max_corrected_codewords
    ):
        status = "low_confidence"
    print(f"status={status}")
    print(f"payload={result.payload_hex}")
    print(f"corrected_codewords={result.corrected_codewords}")
    print(f"bit_confidence={result.bit_confidence:.4f}")
    print(f"sync_confidence={result.sync_confidence:.4f}")
    print(f"pattern_confidence={result.pattern_confidence:.4f}")
    print(f"dft_response={result.dft_response:.4f}")
    print(f"tiles_used={result.tile_count}")
    print(f"tile_px={chosen_tile_px}")
    print(f"best_offset={result.offset_x},{result.offset_y}")


if __name__ == "__main__":
    main()
