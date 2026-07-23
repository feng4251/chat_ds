"""Generate a deterministic PNG above the historical 8 MiB sync boundary."""

from pathlib import Path
import os

import numpy
from PIL import Image


output_root = Path(os.environ["CHATDS_OUTPUT_DIR"])
output_root.mkdir(parents=True, exist_ok=True)
pixels = numpy.random.default_rng(20260723).integers(
    0,
    256,
    size=(2048, 2048, 3),
    dtype=numpy.uint8,
)
destination = output_root / "visual-smoke-over-8mib.png"
Image.fromarray(pixels, mode="RGB").save(
    destination,
    format="PNG",
    compress_level=0,
)
size = destination.stat().st_size
assert 8 * 1024 * 1024 < size < 24 * 1024 * 1024, size
print(f"large-visual-artifact-ok:{size}")
