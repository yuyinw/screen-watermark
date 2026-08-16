from __future__ import annotations

import argparse
from pathlib import Path
import tkinter as tk

from PIL import Image, ImageTk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Click four screen-content corners in a photo.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--max-side", type=int, default=1200)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    image = Image.open(args.image).convert("RGB")
    scale = min(1.0, args.max_side / max(image.width, image.height))
    display = image
    if scale < 1.0:
        display = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.BICUBIC)

    root = tk.Tk()
    root.title("Click TL, TR, BR, BL corners")
    photo = ImageTk.PhotoImage(display)
    canvas = tk.Canvas(root, width=display.width, height=display.height)
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)
    points: list[tuple[float, float]] = []

    def redraw() -> None:
        canvas.delete("mark")
        for index, (x, y) in enumerate(points, start=1):
            sx, sy = x * scale, y * scale
            canvas.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, fill="red", outline="white", tags="mark")
            canvas.create_text(sx + 12, sy - 12, text=str(index), fill="red", tags="mark")
        if len(points) > 1:
            scaled = [(x * scale, y * scale) for x, y in points]
            canvas.create_line(*sum(scaled, ()), fill="red", width=2, tags="mark")

    def on_click(event: tk.Event) -> None:
        if len(points) >= 4:
            return
        points.append((event.x / scale, event.y / scale))
        redraw()
        if len(points) == 4:
            value = " ".join(f"{int(round(x))},{int(round(y))}" for x, y in points)
            print(value)
            root.clipboard_clear()
            root.clipboard_append(value)
            root.after(500, root.destroy)

    def on_key(event: tk.Event) -> None:
        if event.keysym.lower() in {"backspace", "delete"} and points:
            points.pop()
            redraw()
        elif event.keysym.lower() == "escape":
            root.destroy()

    canvas.bind("<Button-1>", on_click)
    root.bind("<Key>", on_key)
    root.mainloop()


if __name__ == "__main__":
    main()
