from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import secrets
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image
from flask import Flask, jsonify, request, send_from_directory

from .core import WatermarkConfig, decode_payload_from_image
from .decode import _auto_screen_corners, _auto_tile_sizes, _infer_warp_size, _order_points, _rectify_with_corners


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web"
UPLOAD_DIR = ROOT.parent / "artifacts" / "trace_uploads"
DB_PATH = ROOT.parent / "artifacts" / "trace_service.sqlite3"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payload_registry (
                payload TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                device TEXT,
                session TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decode_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                image_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                corrected_codewords INTEGER NOT NULL,
                bit_confidence REAL NOT NULL,
                sync_confidence REAL NOT NULL,
                pattern_confidence REAL NOT NULL,
                dft_response REAL NOT NULL,
                tiles_used INTEGER NOT NULL,
                tile_px INTEGER NOT NULL,
                best_offset TEXT NOT NULL,
                warp_size TEXT NOT NULL,
                corners TEXT NOT NULL,
                algorithm TEXT NOT NULL,
                channel TEXT NOT NULL,
                subject TEXT,
                device TEXT,
                session TEXT,
                note TEXT
            )
            """
        )


def _registry_lookup(payload: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM payload_registry WHERE payload = ?", (payload.lower(),)).fetchone()
    return dict(row) if row else None


def _record_attempt(record: dict) -> int:
    fields = [
        "created_at",
        "image_id",
        "status",
        "payload",
        "corrected_codewords",
        "bit_confidence",
        "sync_confidence",
        "pattern_confidence",
        "dft_response",
        "tiles_used",
        "tile_px",
        "best_offset",
        "warp_size",
        "corners",
        "algorithm",
        "channel",
        "subject",
        "device",
        "session",
        "note",
    ]
    placeholders = ", ".join("?" for _ in fields)
    with _connect() as conn:
        cursor = conn.execute(
            f"INSERT INTO decode_attempts ({', '.join(fields)}) VALUES ({placeholders})",
            [record.get(field) for field in fields],
        )
        return int(cursor.lastrowid)


def _image_to_data_url(image: Image.Image, max_side: int = 1200) -> str:
    output = image.convert("RGB")
    output.thumbnail((max_side, max_side), Image.Resampling.BICUBIC)
    buffer = BytesIO()
    output.save(buffer, format="JPEG", quality=88)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _make_config(payload: dict) -> WatermarkConfig:
    camera_mode = bool(payload.get("cameraMode", True))
    algorithm = payload.get("algorithm") or ("fsk" if camera_mode else "pn")
    channel = payload.get("channel") or ("luma" if algorithm == "fsk" else "luma")
    grid_size = int(payload.get("gridSize") or (64 if algorithm == "fsk" else 32))
    tile_size = int(payload.get("tileSize") or 256)
    return WatermarkConfig(
        tile_size=tile_size,
        grid_size=grid_size,
        secret=str(payload.get("secret") or "desktop-watermark-v1"),
        channel=channel,
        algorithm=algorithm,
        smooth_radius=0.4 if algorithm == "fsk" else 0.5,
        sync_strength=0.8 if algorithm == "fsk" else 0.55,
    )


def _dedupe_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    output: list[int] = []
    for value in values:
        ivalue = int(round(value))
        if ivalue > 0 and ivalue not in seen:
            seen.add(ivalue)
            output.append(ivalue)
    return output


def _partial_tile_sizes(image_size: tuple[int, int], nominal_tile_size: int) -> list[int]:
    width, height = image_size
    max_tile = max(64, min(width, height))
    base = [
        nominal_tile_size * 0.75,
        nominal_tile_size,
        nominal_tile_size * 1.145,
    ]
    values = [value for value in base if 64 <= int(round(value)) <= max_tile]
    return _dedupe_ints(sorted(values))


def _decode_best_tile(
    image: Image.Image,
    config: WatermarkConfig,
    payload: dict,
) -> tuple[int, list[int], object]:
    auto_tile = bool(payload.get("autoTile", config.algorithm == "fsk"))
    partial_mode = bool(payload.get("partialMode", False))
    if partial_mode:
        tile_sizes = _partial_tile_sizes(image.size, config.tile_size)
    elif auto_tile:
        tile_sizes = _auto_tile_sizes(
            image.size,
            config.tile_size,
            quick=not bool(payload.get("fullTileScan", False)),
        )
    else:
        tile_sizes = [int(payload.get("tilePx") or config.tile_size)]

    offset_step = int(payload.get("offsetStep") or (32 if config.algorithm == "fsk" else 16))
    max_tiles = int(payload.get("maxTilesPerOffset") or (40 if config.algorithm == "fsk" else 80))
    keep_tiles = int(payload.get("keepTiles") or (28 if config.algorithm == "fsk" else 48))
    results = []
    for tile_px in tile_sizes:
        if tile_px > min(image.size):
            continue
        decoded = decode_payload_from_image(
            image,
            config=config,
            tile_px=tile_px,
            offset_step=offset_step,
            max_tiles_per_offset=max_tiles,
            keep_tiles=keep_tiles,
        )
        results.append((tile_px, decoded))
    if not results:
        raise ValueError("no tile candidates fit the image")
    chosen_tile_px, result = max(
        results,
        key=lambda item: (
            item[1].pattern_confidence,
            -item[1].corrected_codewords,
            item[1].sync_confidence,
            item[1].bit_confidence,
        ),
    )
    return chosen_tile_px, tile_sizes, result


def _confidence_thresholds(
    corner_source: str,
    config: WatermarkConfig,
    payload: dict,
) -> tuple[float, int]:
    partial_source = corner_source in {"original", "partial"}
    default_min_pattern = 0.86 if partial_source else (0.70 if config.algorithm == "fsk" else 0.22)
    default_max_corrected = 2 if partial_source else (4 if config.algorithm == "fsk" else 5)
    min_pattern = float(payload.get("minPatternConfidence") or default_min_pattern)
    max_corrected = int(payload.get("maxCorrectedCodewords") or default_max_corrected)
    return min_pattern, max_corrected


def _passes_confidence(result: object, corner_source: str, config: WatermarkConfig, payload: dict) -> bool:
    min_pattern, max_corrected = _confidence_thresholds(corner_source, config, payload)
    return result.pattern_confidence >= min_pattern and result.corrected_codewords <= max_corrected


def _rank_decode_result(result: object, corner_source: str, config: WatermarkConfig, payload: dict) -> tuple:
    return (
        int(_passes_confidence(result, corner_source, config, payload)),
        -int(result.corrected_codewords),
        float(result.pattern_confidence),
        float(result.sync_confidence),
        float(result.bit_confidence),
    )


def _fast_original_payload(payload: dict) -> dict:
    return {
        **payload,
        "partialMode": True,
        "offsetStep": max(int(payload.get("offsetStep") or 32), 512),
        "maxTilesPerOffset": min(int(payload.get("maxTilesPerOffset") or 40), 6),
        "keepTiles": min(int(payload.get("keepTiles") or 28), 6),
    }


def _strong_original_payload(payload: dict) -> dict:
    return {
        **payload,
        "partialMode": True,
        "offsetStep": min(int(payload.get("offsetStep") or 32), 32),
        "maxTilesPerOffset": max(int(payload.get("maxTilesPerOffset") or 40), 40),
        "keepTiles": max(int(payload.get("keepTiles") or 28), 28),
    }


def _decode_original_fallback(
    image: Image.Image,
    config: WatermarkConfig,
    payload: dict,
) -> tuple[int, list[int], object, dict]:
    fast_payload = _fast_original_payload(payload)
    tile_px, tile_candidates, result = _decode_best_tile(image, config, fast_payload)
    if _passes_confidence(result, "original", config, fast_payload):
        return tile_px, tile_candidates, result, fast_payload

    strong_payload = _strong_original_payload(payload)
    strong_tile_px, strong_tile_candidates, strong_result = _decode_best_tile(image, config, strong_payload)
    if _rank_decode_result(strong_result, "original", config, strong_payload) > _rank_decode_result(
        result,
        "original",
        config,
        fast_payload,
    ):
        return strong_tile_px, strong_tile_candidates, strong_result, strong_payload
    return tile_px, tile_candidates, result, fast_payload


def _decode_diagnosis(
    status: str,
    result: object,
    corner_source: str,
    max_corrected: int,
) -> dict:
    if status == "ok":
        return {
            "code": "ok",
            "message": "水印信号稳定，结果可信。",
            "recommendation": "",
        }

    partial_source = corner_source in {"original", "partial"}
    if partial_source and result.pattern_confidence < 0.80 and result.corrected_codewords > max_corrected:
        return {
            "code": "weak_or_missing_watermark",
            "message": "疑似未包含有效水印，或水印在拍摄、缩放、压缩过程中被明显削弱。",
            "recommendation": "确认桌面覆盖层仍在最上层并等待刷新完成；重新拍摄时尽量保留更多屏幕区域，或提高 --amplitude 后重新生成水印。",
        }
    if result.corrected_codewords > max_corrected:
        return {
            "code": "too_many_corrections",
            "message": "候选载荷需要过多纠错，当前结果不可信。",
            "recommendation": "优先使用全屏自动或手动四角矫正；如果是部分屏，请扩大截图范围并减少模糊。",
        }
    return {
        "code": "low_pattern_confidence",
        "message": "水印图案匹配度不足，当前结果不可信。",
        "recommendation": "检查图片是否确实来自带水印的桌面；必要时提高水印强度或重新拍屏。",
    }


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.post("/api/upload")
    def upload():
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": "缺少图片文件"}), 400
        token = secrets.token_hex(12)
        path = UPLOAD_DIR / f"{token}.jpg"
        image = Image.open(file.stream).convert("RGB")
        image.save(path, quality=95)
        return jsonify(
            {
                "imageId": token,
                "width": image.width,
                "height": image.height,
                "preview": _image_to_data_url(image),
            }
        )

    @app.post("/api/decode")
    def decode():
        payload = request.get_json(force=True)
        image_id = str(payload.get("imageId") or "")
        path = UPLOAD_DIR / f"{image_id}.jpg"
        if not path.exists():
            return jsonify({"error": "图片记录不存在，请重新上传"}), 404

        raw_corners = payload.get("corners") or []
        corners = np.asarray(raw_corners, dtype=np.float32)
        if corners.size and corners.shape != (4, 2):
            return jsonify({"error": "请按左上、右上、右下、左下顺序选择四个角点"}), 400
        trace_mode = str(payload.get("traceMode") or ("manual" if corners.shape == (4, 2) else "auto")).lower()
        if trace_mode not in {"auto", "manual", "partial"}:
            return jsonify({"error": "溯源模式仅支持全屏自动、手动四角或部分屏"}), 400

        image = Image.open(path).convert("RGB")
        if trace_mode == "partial":
            corners = np.empty((0, 2), dtype=np.float32)
            corner_source = "partial"
            warp_size = image.size
            rectified = image
        elif trace_mode == "manual":
            if corners.shape != (4, 2):
                return jsonify({"error": "手动四角模式需要选择四个角点"}), 400
            corner_source = "manual"
            corners = _order_points(corners)
            warp_size = _infer_warp_size(corners, int(payload.get("maxWarpSide") or 2200))
            rectified = _rectify_with_corners(image, corners, warp_size)
        else:
            auto_corners = _auto_screen_corners(image)
            if auto_corners is not None:
                corners = auto_corners.astype(np.float32)
                corner_source = "auto"
                warp_size = _infer_warp_size(corners, int(payload.get("maxWarpSide") or 2200))
                rectified = _rectify_with_corners(image, corners, warp_size)
            else:
                corners = np.empty((0, 2), dtype=np.float32)
                corner_source = "original"
                warp_size = image.size
                rectified = image

        config = _make_config(payload)
        if corner_source in {"original", "partial"}:
            tile_px, tile_candidates, result, decode_payload = _decode_original_fallback(rectified, config, payload)
        else:
            decode_payload = payload
            tile_px, tile_candidates, result = _decode_best_tile(rectified, config, decode_payload)

        min_pattern, max_corrected = _confidence_thresholds(corner_source, config, decode_payload)
        status = "ok"
        if result.pattern_confidence < min_pattern or result.corrected_codewords > max_corrected:
            status = "low_confidence"
        diagnosis = _decode_diagnosis(status, result, corner_source, max_corrected)
        registry = _registry_lookup(result.payload_hex)
        attempt_id = _record_attempt(
            {
                "created_at": _now_iso(),
                "image_id": image_id,
                "status": status,
                "payload": result.payload_hex,
                "corrected_codewords": result.corrected_codewords,
                "bit_confidence": result.bit_confidence,
                "sync_confidence": result.sync_confidence,
                "pattern_confidence": result.pattern_confidence,
                "dft_response": result.dft_response,
                "tiles_used": result.tile_count,
                "tile_px": tile_px,
                "best_offset": json.dumps([result.offset_x, result.offset_y]),
                "warp_size": json.dumps(list(warp_size)),
                "corners": json.dumps(corners.tolist()),
                "algorithm": config.algorithm,
                "channel": config.channel,
                "subject": registry.get("subject") if registry else None,
                "device": registry.get("device") if registry else None,
                "session": registry.get("session") if registry else None,
                "note": registry.get("note") if registry else None,
            }
        )

        return jsonify(
            {
                "attemptId": attempt_id,
                "status": status,
                "payload": result.payload_hex,
                "registry": registry,
                "correctedCodewords": result.corrected_codewords,
                "bitConfidence": result.bit_confidence,
                "syncConfidence": result.sync_confidence,
                "patternConfidence": result.pattern_confidence,
                "dftResponse": result.dft_response,
                "tilesUsed": result.tile_count,
                "tilePx": tile_px,
                "tileCandidates": tile_candidates,
                "bestOffset": [result.offset_x, result.offset_y],
                "warpSize": list(warp_size),
                "corners": corners.tolist(),
                "cornerSource": corner_source,
                "diagnosis": diagnosis,
                "rectifiedPreview": _image_to_data_url(rectified),
            }
        )

    @app.get("/api/registry")
    def registry_list():
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM payload_registry ORDER BY created_at DESC").fetchall()
        return jsonify({"items": [dict(row) for row in rows]})

    @app.post("/api/registry")
    def registry_upsert():
        payload = request.get_json(force=True)
        token = str(payload.get("payload") or "").strip().lower()
        if len(token) != 8:
            return jsonify({"error": "水印载荷必须是 8 位十六进制字符"}), 400
        subject = str(payload.get("subject") or "").strip()
        if not subject:
            return jsonify({"error": "请填写人员或对象"}), 400
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO payload_registry (payload, subject, device, session, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(payload) DO UPDATE SET
                    subject = excluded.subject,
                    device = excluded.device,
                    session = excluded.session,
                    note = excluded.note
                """,
                (
                    token,
                    subject,
                    str(payload.get("device") or "").strip(),
                    str(payload.get("session") or "").strip(),
                    str(payload.get("note") or "").strip(),
                    _now_iso(),
                ),
            )
        return jsonify({"ok": True})

    @app.get("/api/history")
    def history():
        limit = min(200, max(1, int(request.args.get("limit", 50))))
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decode_attempts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["best_offset"] = json.loads(item["best_offset"])
            item["warp_size"] = json.loads(item["warp_size"])
            item["corners"] = json.loads(item["corners"])
            items.append(item)
        return jsonify({"items": items})

    @app.get("/api/stats")
    def stats():
        with _connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM decode_attempts").fetchone()["n"]
            ok = conn.execute("SELECT COUNT(*) AS n FROM decode_attempts WHERE status = 'ok'").fetchone()["n"]
            low = conn.execute("SELECT COUNT(*) AS n FROM decode_attempts WHERE status != 'ok'").fetchone()["n"]
            recent = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_n
                FROM (SELECT status FROM decode_attempts ORDER BY id DESC LIMIT 50)
                """
            ).fetchone()
        return jsonify(
            {
                "total": total,
                "ok": ok,
                "lowConfidence": low,
                "successRate": (ok / total) if total else 0,
                "recentTotal": int(recent["n"] or 0),
                "recentOk": int(recent["ok_n"] or 0),
                "recentSuccessRate": (float(recent["ok_n"] or 0) / float(recent["n"])) if recent["n"] else 0,
            }
        )

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local screen-watermark tracing service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
