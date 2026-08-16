from __future__ import annotations

import argparse
from pathlib import Path

from .core import WatermarkConfig, derive_payload, make_overlay_image, normalize_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a tiled watermark overlay image.")
    payload = parser.add_mutually_exclusive_group(required=True)
    payload.add_argument("--payload", help="32-bit payload as hex, for example 1a2b3c4d.")
    payload.add_argument("--derive-from", help="Derive a 32-bit payload from arbitrary text.")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--grid-size", type=int, default=32)
    parser.add_argument("--secret", default="desktop-watermark-v1")
    parser.add_argument("--channel", choices=["luma", "chroma"], default="luma")
    parser.add_argument("--algorithm", choices=["pn", "fsk"], default="pn")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = derive_payload(args.derive_from, args.secret) if args.derive_from else args.payload
    normalize_payload(payload)
    config = WatermarkConfig(
        tile_size=args.tile_size,
        grid_size=args.grid_size,
        secret=args.secret,
        channel=args.channel,
        algorithm=args.algorithm,
    )
    image = make_overlay_image(payload, args.width, args.height, config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)
    print(f"payload={payload}")
    print(f"wrote={args.out}")


if __name__ == "__main__":
    main()
