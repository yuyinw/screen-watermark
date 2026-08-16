"""Desktop spread-spectrum watermark prototype."""

from .core import WatermarkConfig, decode_payload_from_image, make_overlay_image

__all__ = ["WatermarkConfig", "decode_payload_from_image", "make_overlay_image"]
