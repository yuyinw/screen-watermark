from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageFilter


ENCODED_BITS = 56
PAYLOAD_BITS = 32


@dataclass(frozen=True)
class WatermarkConfig:
    payload_bits: int = PAYLOAD_BITS
    encoded_bits: int = ENCODED_BITS
    tile_size: int = 256
    grid_size: int = 32
    secret: str = "desktop-watermark-v1"
    sync_strength: float = 0.55
    smooth_radius: float = 0.5
    channel: str = "luma"
    algorithm: str = "pn"

    @property
    def chip_count(self) -> int:
        return self.grid_size * self.grid_size

    @property
    def cell_size(self) -> int:
        return self.tile_size // self.grid_size


@dataclass
class TileObservation:
    offset_x: int
    offset_y: int
    tile_x: int
    tile_y: int
    sync_score: float
    bit_scores: np.ndarray
    cells: np.ndarray


@dataclass
class DecodeResult:
    payload_hex: str
    payload_int: int
    corrected_codewords: int
    bit_confidence: float
    sync_confidence: float
    pattern_confidence: float
    tile_count: int
    offset_x: int
    offset_y: int
    encoded_bits: str
    dft_response: float = 0.0


def normalize_payload(payload: str | int) -> int:
    if isinstance(payload, int):
        value = payload
    else:
        text = payload.strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        value = int(text, 16)
    if not 0 <= value < 2**PAYLOAD_BITS:
        raise ValueError("payload must fit in 32 bits")
    return value


def payload_to_hex(payload: int) -> str:
    return f"{payload & 0xFFFFFFFF:08x}"


def derive_payload(text: str, secret: str = "desktop-watermark-v1") -> str:
    digest = hashlib.blake2s(
        text.encode("utf-8"),
        key=secret.encode("utf-8")[:32],
        digest_size=4,
    ).hexdigest()
    return digest


def _payload_bits(payload: int) -> list[int]:
    return [(payload >> shift) & 1 for shift in range(PAYLOAD_BITS - 1, -1, -1)]


def _bits_to_payload(bits: Iterable[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | (int(bit) & 1)
    return value


def _hamming74_encode_nibble(nibble: int) -> list[int]:
    d0 = (nibble >> 3) & 1
    d1 = (nibble >> 2) & 1
    d2 = (nibble >> 1) & 1
    d3 = nibble & 1
    p1 = d0 ^ d1 ^ d3
    p2 = d0 ^ d2 ^ d3
    p4 = d1 ^ d2 ^ d3
    return [p1, p2, d0, p4, d1, d2, d3]


def _hamming74_decode_codeword(codeword: Iterable[int]) -> tuple[list[int], bool]:
    bits = [int(bit) & 1 for bit in codeword]
    s1 = bits[0] ^ bits[2] ^ bits[4] ^ bits[6]
    s2 = bits[1] ^ bits[2] ^ bits[5] ^ bits[6]
    s4 = bits[3] ^ bits[4] ^ bits[5] ^ bits[6]
    syndrome = s1 | (s2 << 1) | (s4 << 2)
    corrected = False
    if syndrome:
        index = syndrome - 1
        if 0 <= index < 7:
            bits[index] ^= 1
            corrected = True
    return [bits[2], bits[4], bits[5], bits[6]], corrected


def encode_payload_bits(payload: int) -> np.ndarray:
    bits = _payload_bits(payload)
    encoded: list[int] = []
    for index in range(0, PAYLOAD_BITS, 4):
        nibble = _bits_to_payload(bits[index : index + 4])
        encoded.extend(_hamming74_encode_nibble(nibble))
    return np.asarray(encoded, dtype=np.int8)


def decode_payload_bits(encoded_bits: Iterable[int]) -> tuple[int, int]:
    encoded = [int(bit) & 1 for bit in encoded_bits]
    if len(encoded) != ENCODED_BITS:
        raise ValueError(f"expected {ENCODED_BITS} encoded bits")
    payload_bits: list[int] = []
    corrected_count = 0
    for index in range(0, ENCODED_BITS, 7):
        decoded, corrected = _hamming74_decode_codeword(encoded[index : index + 7])
        payload_bits.extend(decoded)
        corrected_count += int(corrected)
    return _bits_to_payload(payload_bits), corrected_count


def soft_decode_payload_scores(scores: np.ndarray) -> tuple[int, int, np.ndarray]:
    if len(scores) != ENCODED_BITS:
        raise ValueError(f"expected {ENCODED_BITS} bit scores")
    hard_bits = (scores >= 0).astype(np.int8)
    payload_bits: list[int] = []
    selected_encoded: list[int] = []
    corrected_count = 0

    for index in range(0, ENCODED_BITS, 7):
        chunk = scores[index : index + 7]
        best_score = -math.inf
        best_nibble = 0
        best_code: list[int] = []
        for nibble in range(16):
            code = _hamming74_encode_nibble(nibble)
            signs = np.asarray([bit * 2 - 1 for bit in code], dtype=np.float32)
            score = float(np.dot(signs, chunk))
            if score > best_score:
                best_score = score
                best_nibble = nibble
                best_code = code
        selected_encoded.extend(best_code)
        corrected_count += int(np.count_nonzero(hard_bits[index : index + 7] != np.asarray(best_code)))
        payload_bits.extend(
            [
                (best_nibble >> 3) & 1,
                (best_nibble >> 2) & 1,
                (best_nibble >> 1) & 1,
                best_nibble & 1,
            ]
        )

    return _bits_to_payload(payload_bits), corrected_count, np.asarray(selected_encoded, dtype=np.int8)


def _seed(secret: str, label: str) -> int:
    digest = hashlib.blake2s(
        label.encode("utf-8"),
        key=secret.encode("utf-8")[:32],
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "little")


def _codes(config: WatermarkConfig) -> np.ndarray:
    rng = np.random.default_rng(_seed(config.secret, "message-codes"))
    codes = rng.choice([-1.0, 1.0], size=(config.encoded_bits, config.chip_count))
    codes -= codes.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(codes, axis=1, keepdims=True)
    return codes / np.maximum(norms, 1e-9)


def _sync_code(config: WatermarkConfig) -> np.ndarray:
    rng = np.random.default_rng(_seed(config.secret, "sync-code"))
    sync = rng.choice([-1.0, 1.0], size=config.chip_count)
    sync -= sync.mean()
    sync /= max(float(np.linalg.norm(sync)), 1e-9)
    return sync


def _fsk_rng(config: WatermarkConfig, label: str) -> np.random.Generator:
    return np.random.default_rng(_seed(config.secret, f"fsk-{label}"))


def _fsk_pilot_frequencies() -> list[tuple[int, int]]:
    return [(3, 5), (5, 3), (4, 7), (7, 4), (6, 9), (9, 6)]


def _fsk_frequency_pairs(config: WatermarkConfig) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    pilots = set(_fsk_pilot_frequencies())
    candidates: list[tuple[int, int]] = []
    max_freq = min(14, max(6, config.grid_size // 2 - 2))
    for ky in range(1, max_freq + 1):
        for kx in range(1, max_freq + 1):
            radius = math.hypot(kx, ky)
            if 4.0 <= radius <= max_freq and (kx, ky) not in pilots:
                candidates.append((kx, ky))

    # Pair nearby-radius tones so background texture and optical blur affect both
    # alternatives similarly. The secret only shuffles within coarse radius bins.
    bins: dict[int, list[tuple[int, int]]] = {}
    for freq in candidates:
        bucket = int(round(math.hypot(*freq)))
        bins.setdefault(bucket, []).append(freq)

    rng = _fsk_rng(config, "pairs")
    ordered: list[tuple[int, int]] = []
    for bucket in sorted(bins):
        group = bins[bucket]
        rng.shuffle(group)
        ordered.extend(group)

    pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    index = 0
    while len(pairs) < config.encoded_bits and index + 1 < len(ordered):
        first = ordered[index]
        second = ordered[index + 1]
        if first != second:
            pairs.append((first, second))
        index += 2
    if len(pairs) < config.encoded_bits:
        raise ValueError("not enough FSK frequency pairs")
    return pairs


def _fsk_phase(config: WatermarkConfig, label: str, index: int) -> float:
    rng = _fsk_rng(config, f"{label}-{index}")
    return float(rng.uniform(0.0, 2.0 * math.pi))


def _cosine_tone(tile_size: int, frequency: tuple[int, int], phase: float) -> np.ndarray:
    kx, ky = frequency
    y, x = np.mgrid[0:tile_size, 0:tile_size].astype(np.float32)
    return np.cos(2.0 * math.pi * (kx * x + ky * y) / tile_size + phase).astype(np.float32)


def make_fsk_tile_field(payload: str | int, config: WatermarkConfig | None = None) -> np.ndarray:
    cfg = config or WatermarkConfig(algorithm="fsk")
    payload_int = normalize_payload(payload)
    encoded = encode_payload_bits(payload_int)
    field = np.zeros((cfg.tile_size, cfg.tile_size), dtype=np.float32)
    pairs = _fsk_frequency_pairs(cfg)

    for index, bit in enumerate(encoded):
        frequency = pairs[index][int(bit)]
        phase = _fsk_phase(cfg, "message", index)
        field += _cosine_tone(cfg.tile_size, frequency, phase)

    pilot_weight = max(1.0, cfg.sync_strength * 2.0)
    for index, frequency in enumerate(_fsk_pilot_frequencies()):
        phase = _fsk_phase(cfg, "pilot", index)
        field += pilot_weight * _cosine_tone(cfg.tile_size, frequency, phase)

    field -= field.mean()
    percentile = np.percentile(np.abs(field), 99.0)
    field /= max(float(percentile), 1e-6)
    return np.clip(field, -1.0, 1.0).astype(np.float32)


def make_fsk_sync_field(config: WatermarkConfig | None = None) -> np.ndarray:
    cfg = config or WatermarkConfig(algorithm="fsk")
    field = np.zeros((cfg.tile_size, cfg.tile_size), dtype=np.float32)
    for index, frequency in enumerate(_fsk_pilot_frequencies()):
        phase = _fsk_phase(cfg, "pilot", index)
        field += _cosine_tone(cfg.tile_size, frequency, phase)
    field -= field.mean()
    percentile = np.percentile(np.abs(field), 99.0)
    field /= max(float(percentile), 1e-6)
    return np.clip(field, -1.0, 1.0).astype(np.float32)


def make_tile_field(payload: str | int, config: WatermarkConfig | None = None) -> np.ndarray:
    cfg = config or WatermarkConfig()
    if cfg.algorithm == "fsk":
        return make_fsk_tile_field(payload, cfg)
    if cfg.algorithm != "pn":
        raise ValueError("algorithm must be 'pn' or 'fsk'")
    payload_int = normalize_payload(payload)
    encoded = encode_payload_bits(payload_int)
    signs = (encoded * 2 - 1).astype(np.float32)

    message = signs @ _codes(cfg)
    field = message + cfg.sync_strength * _sync_code(cfg)
    field = field.reshape((cfg.grid_size, cfg.grid_size))
    field -= field.mean()
    percentile = np.percentile(np.abs(field), 98.0)
    field /= max(float(percentile), 1e-6)
    return np.clip(field, -1.0, 1.0).astype(np.float32)


def make_sync_field(config: WatermarkConfig | None = None) -> np.ndarray:
    cfg = config or WatermarkConfig()
    if cfg.algorithm == "fsk":
        return make_fsk_sync_field(cfg)
    if cfg.algorithm != "pn":
        raise ValueError("algorithm must be 'pn' or 'fsk'")
    field = cfg.sync_strength * _sync_code(cfg)
    field = field.reshape((cfg.grid_size, cfg.grid_size))
    field -= field.mean()
    percentile = np.percentile(np.abs(field), 98.0)
    field /= max(float(percentile), 1e-6)
    return np.clip(field, -1.0, 1.0).astype(np.float32)


def _resize_field_tile(field: np.ndarray, tile_size: int, smooth_radius: float) -> np.ndarray:
    if field.shape == (tile_size, tile_size):
        tile_arr = field.astype(np.float32)
        if smooth_radius > 0:
            img = Image.fromarray(((tile_arr + 1.0) * 127.5).astype(np.uint8), "L")
            img = img.filter(ImageFilter.GaussianBlur(radius=smooth_radius))
            tile_arr = (np.asarray(img, dtype=np.float32) - 127.5) / 127.5
        return tile_arr
    small = ((field + 1.0) * 127.5).astype(np.uint8)
    tile = Image.fromarray(small, "L").resize((tile_size, tile_size), Image.Resampling.BICUBIC)
    if smooth_radius > 0:
        tile = tile.filter(ImageFilter.GaussianBlur(radius=smooth_radius))
    return (np.asarray(tile, dtype=np.float32) - 127.5) / 127.5


def make_tiled_field_image(
    payload: str | int,
    width: int,
    height: int,
    config: WatermarkConfig | None = None,
) -> np.ndarray:
    cfg = config or WatermarkConfig()
    tile_arr = _resize_field_tile(make_tile_field(payload, cfg), cfg.tile_size, cfg.smooth_radius)
    output = np.zeros((height, width), dtype=np.float32)
    for y in range(0, height, cfg.tile_size):
        for x in range(0, width, cfg.tile_size):
            h = min(cfg.tile_size, height - y)
            w = min(cfg.tile_size, width - x)
            output[y : y + h, x : x + w] = tile_arr[:h, :w]
    return output


def make_tiled_sync_image(
    width: int,
    height: int,
    tile_px: int,
    config: WatermarkConfig | None = None,
) -> np.ndarray:
    cfg = config or WatermarkConfig()
    tile_arr = _resize_field_tile(make_sync_field(cfg), tile_px, cfg.smooth_radius)
    output = np.zeros((height, width), dtype=np.float32)
    for y in range(0, height, tile_px):
        for x in range(0, width, tile_px):
            h = min(tile_px, height - y)
            w = min(tile_px, width - x)
            output[y : y + h, x : x + w] = tile_arr[:h, :w]
    return output


def make_tile_image(payload: str | int, config: WatermarkConfig | None = None) -> Image.Image:
    cfg = config or WatermarkConfig()
    img_arr = ((make_tiled_field_image(payload, cfg.tile_size, cfg.tile_size, cfg) + 1.0) * 127.5).astype(np.uint8)
    img = Image.fromarray(img_arr, "L")
    if cfg.channel == "chroma":
        arr = np.asarray(img, dtype=np.float32)
        signed = (arr - 127.5) / 127.5
        red = np.clip(128.0 + 127.0 * signed, 0, 255).astype(np.uint8)
        green = np.clip(128.0 - 64.0 * signed, 0, 255).astype(np.uint8)
        blue = np.clip(128.0 - 64.0 * signed, 0, 255).astype(np.uint8)
        return Image.fromarray(np.dstack([red, green, blue]), "RGB")
    if cfg.channel != "luma":
        raise ValueError("channel must be 'luma' or 'chroma'")
    return img.convert("RGB")


def make_overlay_image(
    payload: str | int,
    width: int,
    height: int,
    config: WatermarkConfig | None = None,
) -> Image.Image:
    cfg = config or WatermarkConfig()
    tile = make_tile_image(payload, cfg)
    output = Image.new("RGB", (width, height), (128, 128, 128))
    for y in range(0, height, cfg.tile_size):
        for x in range(0, width, cfg.tile_size):
            output.paste(tile, (x, y))
    return output


def make_adaptive_overlay_image(
    payload: str | int,
    base: Image.Image,
    opacity: float,
    amplitude: float = 5.0,
    config: WatermarkConfig | None = None,
) -> Image.Image:
    if opacity <= 0:
        raise ValueError("opacity must be greater than 0 for adaptive overlay")
    cfg = config or WatermarkConfig()
    base_rgb = np.asarray(base.convert("RGB"), dtype=np.float32)
    field = make_tiled_field_image(payload, base.width, base.height, cfg)

    if cfg.channel == "chroma":
        delta = np.dstack(
            [
                amplitude * field,
                -0.5 * amplitude * field,
                -0.5 * amplitude * field,
            ]
        )
    elif cfg.channel == "luma":
        delta = np.dstack([amplitude * field, amplitude * field, amplitude * field])
    else:
        raise ValueError("channel must be 'luma' or 'chroma'")

    overlay = base_rgb + delta / opacity
    overlay = np.clip(overlay, 0, 255).astype(np.uint8)
    return Image.fromarray(overlay, "RGB")


def apply_overlay_to_image(
    base: Image.Image,
    payload: str | int,
    opacity: float = 0.06,
    config: WatermarkConfig | None = None,
) -> Image.Image:
    cfg = config or WatermarkConfig()
    base_rgb = base.convert("RGB")
    overlay = make_overlay_image(payload, base_rgb.width, base_rgb.height, cfg)
    return Image.blend(base_rgb, overlay, opacity)


def apply_adaptive_overlay_to_image(
    base: Image.Image,
    payload: str | int,
    opacity: float = 0.055,
    amplitude: float = 5.0,
    config: WatermarkConfig | None = None,
) -> Image.Image:
    cfg = config or WatermarkConfig()
    base_rgb = base.convert("RGB")
    overlay = make_adaptive_overlay_image(payload, base_rgb, opacity, amplitude, cfg)
    return Image.blend(base_rgb, overlay, opacity)


def _image_to_signal_array(
    image: Image.Image | np.ndarray | str | Path,
    config: WatermarkConfig,
) -> np.ndarray:
    if isinstance(image, np.ndarray):
        arr = image
    else:
        if isinstance(image, (str, Path)):
            image = Image.open(image)
        arr = np.asarray(image.convert("RGB"))
    arr = arr.astype(np.float32)
    if arr.ndim == 2:
        return arr
    rgb = arr[..., :3]
    if config.channel == "chroma":
        return 2.0 * rgb[..., 0] - rgb[..., 1] - rgb[..., 2] + 128.0
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def _image_to_gray_array(image: Image.Image | np.ndarray | str | Path) -> np.ndarray:
    return _image_to_signal_array(image, WatermarkConfig())


def _tile_to_cells(tile: np.ndarray, grid_size: int) -> np.ndarray:
    import cv2

    cells = cv2.resize(tile.astype(np.float32), (grid_size, grid_size), interpolation=cv2.INTER_AREA)
    return cells.reshape(-1)


def _normalize_cells(cells: np.ndarray) -> np.ndarray:
    cells = cells.astype(np.float32, copy=True)
    cells -= cells.mean()
    std = float(cells.std())
    if std < 1e-6:
        return np.zeros_like(cells)
    cells /= std
    return cells


def _observe_tile(
    tile: np.ndarray,
    offset_x: int,
    offset_y: int,
    tile_x: int,
    tile_y: int,
    config: WatermarkConfig,
) -> TileObservation:
    cells = _tile_to_cells(tile, config.grid_size)
    normalized = _normalize_cells(cells)
    sync = _sync_code(config)
    codes = _codes(config)
    sync_score = float(np.dot(normalized, sync))
    bit_scores = codes @ normalized
    return TileObservation(offset_x, offset_y, tile_x, tile_y, sync_score, bit_scores, cells)


def _iter_tiles(
    gray: np.ndarray,
    tile_px: int,
    offset_x: int,
    offset_y: int,
    max_tiles: int,
) -> Iterable[tuple[int, int, np.ndarray]]:
    height, width = gray.shape[:2]
    yielded = 0
    for y in range(offset_y, height - tile_px + 1, tile_px):
        for x in range(offset_x, width - tile_px + 1, tile_px):
            yield x, y, gray[y : y + tile_px, x : x + tile_px]
            yielded += 1
            if yielded >= max_tiles:
                return


def _scan_offset(
    gray: np.ndarray,
    tile_px: int,
    offset_x: int,
    offset_y: int,
    config: WatermarkConfig,
    max_tiles: int,
) -> tuple[float, list[TileObservation]]:
    observations = [
        _observe_tile(tile, offset_x, offset_y, x, y, config)
        for x, y, tile in _iter_tiles(gray, tile_px, offset_x, offset_y, max_tiles)
    ]
    if not observations:
        return 0.0, []
    cell_stack = np.vstack([obs.cells for obs in observations])
    aggregate = _normalize_cells(cell_stack.mean(axis=0))
    sync_score = float(np.dot(aggregate, _sync_code(config)))
    return abs(sync_score), observations


def _scan_offset_cells(
    signal: np.ndarray,
    tile_px: int,
    offset_x: int,
    offset_y: int,
    config: WatermarkConfig,
    max_tiles: int,
) -> list[np.ndarray]:
    cells: list[np.ndarray] = []
    for _, _, tile in _iter_tiles(signal, tile_px, offset_x, offset_y, max_tiles):
        cells.append(_tile_to_cells(tile, config.grid_size))
    return cells


def _dft_offset_candidates(
    signal: np.ndarray,
    tile_px: int,
    config: WatermarkConfig,
) -> tuple[list[tuple[int, int]], float]:
    import cv2

    if signal.shape[0] < tile_px or signal.shape[1] < tile_px:
        return [], 0.0

    observed = signal.astype(np.float32)
    observed = observed - cv2.GaussianBlur(observed, (0, 0), max(3.0, tile_px / 16.0))
    observed = observed - observed.mean()
    observed /= max(float(observed.std()), 1e-6)

    reference = make_tiled_sync_image(signal.shape[1], signal.shape[0], tile_px, config)
    reference = reference - reference.mean()
    reference /= max(float(reference.std()), 1e-6)

    window = cv2.createHanningWindow((signal.shape[1], signal.shape[0]), cv2.CV_32F)
    shift, response = cv2.phaseCorrelate(reference * window, observed * window)
    sx, sy = shift

    candidates: list[tuple[int, int]] = []
    for base_x, base_y in [
        (int(round(sx)) % tile_px, int(round(sy)) % tile_px),
        (int(round(-sx)) % tile_px, int(round(-sy)) % tile_px),
    ]:
        for dy in (-8, 0, 8):
            for dx in (-8, 0, 8):
                candidates.append(((base_x + dx) % tile_px, (base_y + dy) % tile_px))
    return candidates, float(response)


def _aggregate_decode(
    observations: list[TileObservation],
    config: WatermarkConfig,
    keep_tiles: int,
) -> tuple[np.ndarray, float, float, float, int, int, float, np.ndarray]:
    ordered = sorted(observations, key=lambda obs: abs(obs.sync_score), reverse=True)
    selected = ordered[: max(1, min(keep_tiles, len(ordered)))]
    aggregate = _normalize_cells(np.vstack([obs.cells for obs in selected]).mean(axis=0))
    sync_score = float(np.dot(aggregate, _sync_code(config)))
    polarity = 1.0 if sync_score >= 0 else -1.0
    bit_sum = polarity * (_codes(config) @ aggregate)
    payload, corrected, encoded_bits = soft_decode_payload_scores(bit_sum)
    bit_confidence = float(np.median(np.abs(bit_sum)) / (np.std(bit_sum) + 1e-6))
    expected = _normalize_cells(make_tile_field(payload, config).reshape(-1))
    pattern_confidence = float(np.dot(aggregate, expected) / config.chip_count)
    return bit_sum, sync_score, bit_confidence, pattern_confidence, payload, corrected, float(len(selected)), encoded_bits


def _fsk_prepare_tile(tile: np.ndarray) -> np.ndarray:
    resized = tile.astype(np.float32)
    resized -= resized.mean()
    std = float(resized.std())
    if std < 1e-6:
        return np.zeros_like(resized, dtype=np.float32)
    resized /= std
    height, width = resized.shape[:2]
    if height != width:
        import cv2

        size = min(height, width)
        resized = cv2.resize(resized, (size, size), interpolation=cv2.INTER_AREA)
    return resized.astype(np.float32)


@lru_cache(maxsize=4096)
def _fsk_basis_cached(size: int, kx: int, ky: int, phase_key: int) -> np.ndarray:
    phase = phase_key / 1_000_000.0
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    basis = np.cos(2.0 * math.pi * (kx * x + ky * y) / size + phase).astype(np.float32)
    basis -= basis.mean()
    basis /= max(float(np.linalg.norm(basis)), 1e-6)
    return basis


def _fsk_basis(size: int, frequency: tuple[int, int], phase: float) -> np.ndarray:
    phase_key = int(round((phase % (2.0 * math.pi)) * 1_000_000))
    return _fsk_basis_cached(size, frequency[0], frequency[1], phase_key)


def _fsk_match(tile: np.ndarray, frequency: tuple[int, int], phase: float) -> float:
    prepared = _fsk_prepare_tile(tile)
    if prepared.size == 0:
        return 0.0
    basis = _fsk_basis(prepared.shape[0], frequency, phase)
    return float(np.sum(prepared * basis))


def _fsk_match_prepared(prepared: np.ndarray, frequency: tuple[int, int], phase: float) -> float:
    if prepared.size == 0:
        return 0.0
    basis = _fsk_basis(prepared.shape[0], frequency, phase)
    return float(np.sum(prepared * basis))


def _aggregate_decode_fsk(
    observations: list[TileObservation] | None,
    config: WatermarkConfig,
    keep_tiles: int,
    cells: list[np.ndarray] | None = None,
) -> tuple[np.ndarray, float, float, float, int, int, float, np.ndarray, float, float]:
    if cells is None:
        if observations is None:
            raise ValueError("FSK decode needs observations or cells")
        ordered = sorted(observations, key=lambda obs: float(np.std(obs.cells)), reverse=True)
        selected_cells = [obs.cells for obs in ordered[: max(1, min(keep_tiles, len(ordered)))]]
    else:
        selected_cells = sorted(cells, key=lambda item: float(np.std(item)), reverse=True)
        selected_cells = selected_cells[: max(1, min(keep_tiles, len(selected_cells)))]
    pairs = _fsk_frequency_pairs(config)
    per_tile_scores: list[np.ndarray] = []
    pilot_scores: list[float] = []

    for cell_values in selected_cells:
        tile = cell_values.reshape((config.grid_size, config.grid_size))
        prepared = _fsk_prepare_tile(tile)
        tile_scores = np.zeros(config.encoded_bits, dtype=np.float64)
        for index, (freq0, freq1) in enumerate(pairs):
            phase = _fsk_phase(config, "message", index)
            tile_scores[index] = _fsk_match_prepared(prepared, freq1, phase) - _fsk_match_prepared(
                prepared,
                freq0,
                phase,
            )
        for index, frequency in enumerate(_fsk_pilot_frequencies()):
            pilot_scores.append(abs(_fsk_match_prepared(prepared, frequency, _fsk_phase(config, "pilot", index))))
        scale = float(np.median(np.abs(tile_scores)))
        if scale > 1e-6:
            per_tile_scores.append(tile_scores / scale)

    if not per_tile_scores:
        raise ValueError("no usable FSK tiles")

    score_stack = np.vstack(per_tile_scores)
    mean_scores = score_stack.mean(axis=0)
    median_scores = np.median(score_stack, axis=0)
    if len(per_tile_scores) <= 8:
        scores = 0.45 * mean_scores + 0.55 * median_scores
    else:
        scores = 0.65 * mean_scores + 0.35 * median_scores

    payload, corrected, encoded_bits = soft_decode_payload_scores(scores)
    bit_confidence = float(np.median(np.abs(scores)) / (np.std(scores) + 1e-6))
    sync_confidence = float(np.mean(pilot_scores) / (np.std(pilot_scores) + 1e-6)) if pilot_scores else 0.0
    expected = encode_payload_bits(payload)
    expected_signs = expected * 2 - 1
    pattern_confidence = float(np.dot(scores, expected_signs) / (np.linalg.norm(scores) * math.sqrt(len(scores)) + 1e-6))
    exact_votes = 0
    for tile_score in score_stack:
        tile_payload, _, _ = soft_decode_payload_scores(tile_score)
        exact_votes += int(tile_payload == payload)
    exact_vote_fraction = exact_votes / max(1, len(per_tile_scores))
    bit_agreement = float(np.mean((score_stack >= 0) == (expected_signs >= 0)))
    return (
        scores,
        sync_confidence,
        bit_confidence,
        pattern_confidence,
        payload,
        corrected,
        float(len(selected_cells)),
        encoded_bits,
        exact_vote_fraction,
        bit_agreement,
    )


def _decode_payload_from_image_fsk(
    image: Image.Image | np.ndarray | str | Path,
    config: WatermarkConfig,
    tile_px: int,
    offset_step: int | None,
    max_tiles_per_offset: int,
    keep_tiles: int,
    use_dft_sync: bool,
) -> DecodeResult:
    signal = _image_to_signal_array(image, config)
    if signal.shape[0] < tile_px or signal.shape[1] < tile_px:
        raise ValueError("image is smaller than one watermark tile")

    step = int(offset_step or max(32, tile_px // 4))
    max_ox = min(tile_px, signal.shape[1] - tile_px + 1)
    max_oy = min(tile_px, signal.shape[0] - tile_px + 1)
    candidates: list[tuple[int, int]] = [(0, 0)]
    dft_response = 0.0
    if use_dft_sync:
        dft_candidates, dft_response = _dft_offset_candidates(signal, tile_px, config)
        candidates.extend(dft_candidates[:6])
    for oy in range(0, max_oy, step):
        for ox in range(0, max_ox, step):
            candidates.append((ox, oy))

    best_score = -math.inf
    best_offset = (0, 0)
    best_candidate: tuple[np.ndarray, float, float, float, int, int, float, np.ndarray] | None = None
    visited: set[tuple[int, int]] = set()
    scored: list[tuple[float, int, int]] = []

    def evaluate(ox: int, oy: int) -> None:
        nonlocal best_score, best_offset, best_candidate
        key = (ox, oy)
        if key in visited:
            return
        if not (0 <= ox < max_ox and 0 <= oy < max_oy):
            return
        visited.add(key)
        cell_values = _scan_offset_cells(signal, tile_px, ox, oy, config, max_tiles_per_offset)
        if not cell_values:
            return
        candidate = _aggregate_decode_fsk(None, config, keep_tiles, cells=cell_values)
        (
            _,
            sync_score,
            bit_confidence,
            pattern_confidence,
            _,
            corrected,
            tile_count,
            _,
            exact_vote_fraction,
            bit_agreement,
        ) = candidate
        score = (
            2.4 * pattern_confidence
            + 0.04 * bit_confidence
            + 0.01 * sync_score
            - 0.08 * corrected
            + 0.002 * tile_count
            + 0.35 * exact_vote_fraction
            + 0.20 * bit_agreement
        )
        if tile_count >= 4 and exact_vote_fraction <= 0.0:
            score -= 0.12
        scored.append((score, ox, oy))
        if score > best_score:
            best_score = score
            best_offset = key
            best_candidate = candidate

    for ox, oy in candidates:
        evaluate(ox, oy)

    if step > 8 and scored and (signal.shape[0] < tile_px * 4 or signal.shape[1] < tile_px * 4):
        refine_step = 8
        radius = min(step, max(16, tile_px // 8))
        for _, coarse_x, coarse_y in sorted(scored, reverse=True)[:8]:
            for oy in range(coarse_y - radius, coarse_y + radius + 1, refine_step):
                for ox in range(coarse_x - radius, coarse_x + radius + 1, refine_step):
                    evaluate(ox, oy)

    if best_candidate is None:
        raise ValueError("no decodable watermark candidates found")

    _, sync_score, bit_conf, pattern_conf, payload, corrected, tile_count, encoded_bits, _, _ = best_candidate
    return DecodeResult(
        payload_hex=payload_to_hex(payload),
        payload_int=payload,
        corrected_codewords=corrected,
        bit_confidence=bit_conf,
        sync_confidence=sync_score,
        pattern_confidence=pattern_conf,
        tile_count=int(tile_count),
        offset_x=best_offset[0],
        offset_y=best_offset[1],
        encoded_bits="".join(str(int(bit)) for bit in encoded_bits),
        dft_response=dft_response,
    )


def decode_payload_from_image(
    image: Image.Image | np.ndarray | str | Path,
    config: WatermarkConfig | None = None,
    tile_px: int | None = None,
    offset_step: int | None = None,
    max_tiles_per_offset: int = 80,
    keep_tiles: int = 48,
    use_dft_sync: bool = True,
) -> DecodeResult:
    cfg = config or WatermarkConfig()
    tile = int(tile_px or cfg.tile_size)
    if cfg.algorithm == "fsk":
        return _decode_payload_from_image_fsk(
            image,
            cfg,
            tile,
            offset_step,
            max_tiles_per_offset,
            keep_tiles,
            use_dft_sync,
        )
    gray = _image_to_signal_array(image, cfg)
    if gray.shape[0] < tile or gray.shape[1] < tile:
        raise ValueError("image is smaller than one watermark tile")

    step = int(offset_step or max(8, tile // 16))
    best_score = -1.0
    best_observations: list[TileObservation] = []
    best_offset = (0, 0)
    best_candidate: tuple[np.ndarray, float, float, float, int, int, float, np.ndarray] | None = None
    scored_offsets: list[tuple[float, int, int]] = []
    visited: set[tuple[int, int]] = set()
    dft_response = 0.0

    def evaluate_offset(ox: int, oy: int) -> None:
        nonlocal best_score, best_observations, best_offset, best_candidate
        max_ox = min(tile, gray.shape[1] - tile + 1)
        max_oy = min(tile, gray.shape[0] - tile + 1)
        if not (0 <= ox < max_ox and 0 <= oy < max_oy):
            return
        key = (ox, oy)
        if key in visited:
            return
        visited.add(key)
        _, observations = _scan_offset(gray, tile, ox, oy, cfg, max_tiles_per_offset)
        if not observations:
            return
        if cfg.algorithm == "fsk":
            candidate = _aggregate_decode_fsk(observations, cfg, keep_tiles)
        else:
            candidate = _aggregate_decode(observations, cfg, keep_tiles)
        _, sync_score, bit_confidence, pattern_confidence, _, corrected, tile_count, _, *extra = candidate
        score = (
            1.8 * pattern_confidence
            + 0.04 * bit_confidence
            + 0.03 * abs(sync_score)
            - 0.04 * corrected
            + 0.002 * tile_count
        )
        scored_offsets.append((score, ox, oy))
        if score > best_score:
            best_score = score
            best_observations = observations
            best_offset = (ox, oy)
            best_candidate = candidate

    if use_dft_sync:
        dft_candidates, dft_response = _dft_offset_candidates(gray, tile, cfg)
        for ox, oy in dft_candidates:
            evaluate_offset(ox, oy)

    for oy in range(0, min(tile, gray.shape[0] - tile + 1), step):
        for ox in range(0, min(tile, gray.shape[1] - tile + 1), step):
            evaluate_offset(ox, oy)

    if step > 4 and scored_offsets:
        refine_step = max(2, step // 4)
        radius = step
        for _, coarse_x, coarse_y in sorted(scored_offsets, reverse=True)[:8]:
            for oy in range(coarse_y - radius, coarse_y + radius + 1, refine_step):
                for ox in range(coarse_x - radius, coarse_x + radius + 1, refine_step):
                    evaluate_offset(ox, oy)

    if not best_observations:
        raise ValueError("no complete watermark tiles found")

    if best_candidate is None:
        raise ValueError("no decodable watermark candidates found")
    _, sync_score, bit_conf, pattern_conf, payload, corrected, tile_count, encoded_bits = best_candidate
    sync_conf = abs(sync_score)
    return DecodeResult(
        payload_hex=payload_to_hex(payload),
        payload_int=payload,
        corrected_codewords=corrected,
        bit_confidence=bit_conf,
        sync_confidence=sync_conf,
        pattern_confidence=pattern_conf,
        tile_count=int(tile_count),
        offset_x=best_offset[0],
        offset_y=best_offset[1],
        encoded_bits="".join(str(int(bit)) for bit in encoded_bits),
        dft_response=dft_response,
    )
