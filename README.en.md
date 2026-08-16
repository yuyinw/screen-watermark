# Screen-Camera Desktop Watermark

> A Windows desktop invisible watermark prototype for screen-camera tracing. It embeds a robust tiled watermark into the desktop and recovers a 32-bit trace payload from screenshots, phone photos, partial-screen crops, perspective distortion, and difficult lighting.

[中文 README](README.md) | [Technical Design](docs/technical-design.md)

## Algorithm Overview

The current implementation is a full-screen tiled blind watermark. Each tile independently carries the same 32-bit payload. The payload is protected with Hamming(7,4) error correction, mapped to keyed low-frequency FSK tone pairs, and blended into the desktop as an adaptive residual. The decoder recovers the payload through screen rectification, multi-scale tile search, frequency-domain matched filtering, and multi-tile voting.

## Highlights

- Click-through Windows desktop overlay
- Screenshot, phone-camera, and partial-screen tracing
- Automatic screen detection, manual corner rectification, and borderless partial decode
- Coherent low-frequency FSK watermark designed for display/camera resampling
- Hamming(7,4) error correction and multi-tile voting
- Local web trace console with upload, registry, history, and success statistics
- Low-confidence diagnostics to separate trusted payloads from weak candidates

## Pipeline

```text
32-bit payload
  -> Hamming(7,4) ECC
  -> keyed low-frequency FSK tone pairs
  -> adaptive full-screen residual watermark
  -> screenshot / camera photo / partial crop
  -> rectification or partial-image search
  -> matched filtering + tile voting
  -> trusted payload or low-confidence diagnosis
```

## Installation

```powershell
python -m pip install -r requirements.txt
```

For editable command-line entry points:

```powershell
python -m pip install -e .
```

## Quick Self-Test

```powershell
python -m screen_watermark.selftest
```

The self-test builds synthetic desktop images, embeds the watermark, and verifies full-frame, crop, scale, blur, chroma, and FSK camera-mode recovery.

## Start The Desktop Overlay

For phone-camera tests, use camera mode:

```powershell
python -m screen_watermark.overlay --payload 1a2b3c4e --camera-mode
```

Camera mode defaults:

```text
opacity   >= 0.055
amplitude >= 12.0
tile-size = 256
grid-size = 64
refresh   = 2 seconds
```

For difficult backgrounds or longer camera distance:

```powershell
python -m screen_watermark.overlay --payload 1a2b3c4e --camera-mode --amplitude 14
```

Parameter notes:

- `--opacity`: opacity of the transparent overlay window.
- `--amplitude`: target pixel-level residual strength after blending; this is the main robustness knob.
- `--refresh-interval`: adaptive overlay refresh interval, useful when desktop content changes.

## Decode From CLI

Automatic screen detection and rectification:

```powershell
python -m screen_watermark.decode photo.jpg --camera-mode --auto-screen
```

Manual screen corners:

```powershell
python -m screen_watermark.decode photo.jpg --camera-mode --corners "120,80 1820,140 1760,1010 90,960"
```

Click the four corners interactively:

```powershell
python -m screen_watermark.pick_corners photo.jpg
```

Corner order is top-left, top-right, bottom-right, bottom-left. The service can reorder points, but consistent input reduces edge cases.

## Local Trace Console

```powershell
python -m screen_watermark.trace_server --port 8766
```

Open:

```text
http://127.0.0.1:8766/
```

The web console supports three trace modes:

- **Auto Full Screen**: detect the screen border and rectify the photo.
- **Manual Corners**: rectify from user-selected corners.
- **Partial Screen**: decode directly from a crop without relying on borders.

Trace attempts are stored locally in SQLite:

```text
artifacts/trace_service.sqlite3
```

Only `status=ok` should be treated as a trusted trace. `low_confidence` is a diagnostic candidate, not proof.

## Project Layout

```text
screen_watermark/
  core.py          # watermark generation, FSK decode, ECC, voting
  overlay.py       # Windows transparent desktop overlay
  decode.py        # offline CLI decoder
  trace_server.py  # Flask local trace service
  web/             # Chinese web console
docs/
  technical-design.md
```

Runtime artifacts, uploaded images, SQLite history, and self-test images are written to `artifacts/` and ignored by Git.

## Practical Advice

- Use `--camera-mode` for screen-camera tracing.
- Try `--amplitude 14` or `16` for complex wallpapers, strong lighting, or long-distance photos.
- Partial-screen crops should contain several complete watermark tiles whenever possible.
- If the service reports weak or missing watermark, confirm the overlay is still topmost and wait for one refresh cycle before capturing again.
- In production, map the 32-bit payload to server-side user, device, session, and time metadata. Do not encode personal data directly.

## Scope

This is a practical prototype, not a complete commercial DLP system. It is useful for internal traceability experiments, product PoCs, and robustness research. Production deployment should add process protection, policy enforcement, key rotation, tamper resistance, audit workflows, and human review for low-confidence cases.
