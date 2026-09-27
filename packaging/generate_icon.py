"""Render the editable product mark, including simplified small-size representations."""

from pathlib import Path
import argparse
import subprocess
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent


def generate(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    iconset = output / "MegaProg.iconset"
    iconset.mkdir(exist_ok=True)
    for logical in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            size = logical * scale
            svg = (
                ROOT
                / "assets"
                / ("megaprog-small.svg" if logical <= 32 else "megaprog.svg")
            )
            renderer = QSvgRenderer(str(svg))
            if not renderer.isValid():
                raise ValueError("Invalid icon SVG")
            image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            name = "icon_%dx%d%s.png" % (logical, logical, "@2x" if scale == 2 else "")
            if not image.save(str(iconset / name)):
                raise OSError("Cannot save icon")
            if size == 1024:
                image.save(str(output / "megaprog.png"))
    subprocess.run(
        [
            "/usr/bin/iconutil",
            "-c",
            "icns",
            str(iconset),
            "-o",
            str(output / "megaprog.icns"),
        ],
        check=True,
    )
    return output / "megaprog.icns"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    print(generate(parser.parse_args().output))
