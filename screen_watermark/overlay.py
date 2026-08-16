from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import signal
import sys
import tempfile
import tkinter as tk
import time

from PIL import ImageTk

from PIL import ImageGrab

from .core import WatermarkConfig, derive_payload, make_adaptive_overlay_image, make_overlay_image, normalize_payload


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _virtual_screen() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return x, y, width, height


def _make_click_through(window: tk.Tk) -> None:
    hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show a click-through Windows desktop watermark overlay.")
    payload = parser.add_mutually_exclusive_group(required=True)
    payload.add_argument("--payload", help="32-bit payload as hex, for example 1a2b3c4d.")
    payload.add_argument("--derive-from", help="Derive a 32-bit payload from arbitrary text.")
    parser.add_argument("--opacity", type=float, default=0.055, help="Global overlay opacity, usually 0.04-0.08.")
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--grid-size", type=int, default=32)
    parser.add_argument("--secret", default="desktop-watermark-v1")
    parser.add_argument("--channel", choices=["luma", "chroma"], default="luma")
    parser.add_argument("--algorithm", choices=["pn", "fsk"], default="pn")
    parser.add_argument("--adaptive", action="store_true", help="Capture the desktop once and build an additive residual overlay.")
    parser.add_argument("--amplitude", type=float, default=5.0, help="Target final-pixel residual for --adaptive.")
    parser.add_argument("--camera-mode", action="store_true", help="Preset for phone photos: adaptive low-frequency FSK residual.")
    parser.add_argument(
        "--refresh-interval",
        type=float,
        default=0.0,
        help="For adaptive mode, recapture the desktop and rebuild the overlay every N seconds. Use 0 to disable.",
    )
    parser.add_argument("--save-overlay", type=Path, help="Optionally save the generated overlay image.")
    return parser.parse_args()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("The overlay is Windows-only.")
    args = parse_args()
    if args.camera_mode:
        args.algorithm = "fsk"
        args.channel = "luma"
        args.adaptive = True
        args.amplitude = max(args.amplitude, 12.0)
        args.opacity = max(args.opacity, 0.055)
        args.tile_size = 256
        args.grid_size = 64
        if args.refresh_interval <= 0:
            args.refresh_interval = 2.0

    payload = derive_payload(args.derive_from, args.secret) if args.derive_from else args.payload
    normalize_payload(payload)
    x, y, width, height = _virtual_screen()

    config = WatermarkConfig(
        tile_size=args.tile_size,
        grid_size=args.grid_size,
        secret=args.secret,
        channel=args.channel,
        algorithm=args.algorithm,
    )
    def build_overlay() -> object:
        if args.adaptive:
            base = ImageGrab.grab(bbox=(x, y, x + width, y + height))
            return make_adaptive_overlay_image(payload, base, args.opacity, args.amplitude, config)
        return make_overlay_image(payload, width, height, config)

    overlay = build_overlay()
    if args.save_overlay:
        args.save_overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.save(args.save_overlay)

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", max(0.0, min(1.0, args.opacity)))
    root.geometry(f"{width}x{height}{x:+d}{y:+d}")
    root.configure(bg="black")

    photo = ImageTk.PhotoImage(overlay)
    label = tk.Label(root, image=photo, borderwidth=0, highlightthickness=0)
    label.image = photo
    label.pack(fill="both", expand=True)

    root.update_idletasks()
    _make_click_through(root)
    stopped = False

    def keep_topmost() -> None:
        if stopped or not root.winfo_exists():
            return
        try:
            root.attributes("-topmost", True)
            root.after(3000, keep_topmost)
        except tk.TclError:
            return

    def refresh_overlay() -> None:
        if stopped or not args.adaptive or args.refresh_interval <= 0 or not root.winfo_exists():
            return
        try:
            root.attributes("-alpha", 0.0)
            root.update_idletasks()
            root.update()
            time.sleep(0.05)
            refreshed = build_overlay()
            refreshed_photo = ImageTk.PhotoImage(refreshed)
            label.configure(image=refreshed_photo)
            label.image = refreshed_photo
            root.attributes("-alpha", max(0.0, min(1.0, args.opacity)))
            root.attributes("-topmost", True)
            root.after(int(args.refresh_interval * 1000), refresh_overlay)
        except tk.TclError:
            return

    def stop(*_: object) -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        try:
            root.destroy()
        except tk.TclError:
            pass

    signal.signal(signal.SIGINT, stop)
    keep_topmost()
    if args.adaptive and args.refresh_interval > 0:
        root.after(int(args.refresh_interval * 1000), refresh_overlay)
    print(f"payload={payload}")
    print(f"overlay={width}x{height}{x:+d}{y:+d}")
    print(
        f"algorithm={args.algorithm} channel={args.channel} adaptive={args.adaptive} "
        f"amplitude={args.amplitude} opacity={args.opacity} refresh_interval={args.refresh_interval}"
    )
    print("Press Ctrl+C in this terminal to stop.")
    root.mainloop()


if __name__ == "__main__":
    main()
