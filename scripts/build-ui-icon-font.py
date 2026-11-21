#!/usr/bin/env python3
"""Generate the Mate UI icon font from the pinned Material Design Icons SVG package.

Run with FontTools and Brotli installed:
    npm pack @mdi/svg@7.4.47
    python scripts/build-ui-icon-font.py mdi-svg-7.4.47.tgz
"""

import argparse
import json
import tarfile
from pathlib import Path
from xml.etree import ElementTree

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.svgLib.path import parse_path
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

# UI glyphs appear in templates, translated strings and dynamically rendered
# labels. The same Unicode characters keep their text and semantics in all three.
ICONS = {
    "⚡": "lightning-bolt",
    "🗺": "map-outline",
    "🌍🌐": "earth",
    "🔌": "power-plug",
    "⛽": "gas-station",
    "🛢": "barrel",
    "💰": "cash-multiple",
    "📊": "chart-bar",
    "📈": "chart-line",
    "🕒🕘": "clock-outline",
    "⏱": "timer-outline",
    "📆📅": "calendar-month-outline",
    "🩺": "stethoscope",
    "🎮": "gamepad-variant-outline",
    "🚗": "car",
    "🔋": "battery",
    "⚙": "cog-outline",
    "🚪": "door",
    "🔑": "key-variant",
    "🔐🔒": "lock-outline",
    "🔓": "lock-open-outline",
    "💤": "sleep",
    "⚠": "alert-outline",
    "ℹ": "information-outline",
    "❓": "help-circle-outline",
    "⏳⌛": "timer-sand",
    "🔄": "refresh",
    "♻": "recycle",
    "🔍🔎": "magnify",
    "✎✏": "pencil-outline",
    "🔗": "link-variant",
    "🧭": "compass-outline",
    "📍": "map-marker-outline",
    "✨": "creation",
    "📝": "note-text-outline",
    "💶": "cash",
    "🧾": "receipt-text-outline",
    "🗑": "trash-can-outline",
    "🆓": "tag-outline",
    "💡": "lightbulb-outline",
    "🏠": "home-outline",
    "☀": "weather-sunny",
    "⏸": "pause-circle-outline",
    "🚀": "rocket-launch-outline",
    "➕": "plus",
    "✕✗": "close",
    "✓✔✅": "check",
    "🛣": "road-variant",
    "📡": "satellite-uplink",
    "🌡": "thermometer",
    "🔢": "numeric",
    "📒": "notebook-outline",
    "☕": "coffee-outline",
    "📶": "signal",
    "⚪🟢🟡🔴": "circle-medium",
    "❄": "snowflake",
    "🔥": "fire",
    "🧊": "cube-outline",
    "🖥": "monitor",
    "⬇": "download",
    "☁": "cloud-outline",
    "🏔": "terrain",
    "🎛": "tune-variant",
    "🔀": "shuffle-variant",
    "🎯": "target",
    "🦇": "bat",
    "👍": "thumb-up-outline",
    "🪟": "window-closed",
    "📲": "cellphone-arrow-down",
    "📦": "package-variant-closed",
    "⛔": "cancel",
    "↩": "undo",
}


def build_font(package, output):
    with tarfile.open(package) as source:
        metadata = json.load(source.extractfile("package/package.json"))
        if (metadata["name"], metadata["version"]) != ("@mdi/svg", "7.4.47"):
            raise ValueError("Expected @mdi/svg version 7.4.47")

        glyphs = {".notdef": TTGlyphPen(None).glyph()}
        for name in sorted(set(ICONS.values())):
            svg = ElementTree.fromstring(source.extractfile(f"package/svg/{name}.svg").read())
            pen = TTGlyphPen(None)
            outline = TransformPen(Cu2QuPen(pen, max_err=0.5), (35, 0, 0, -35, 80, 770))
            for path in svg.findall("{http://www.w3.org/2000/svg}path"):
                parse_path(path.attrib["d"], outline)
            glyphs[name] = pen.glyph()
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(list(glyphs))
    cmap = {ord(char): name for chars, name in ICONS.items() for char in chars}
    builder.setupCharacterMap(cmap)

    # Provide both Unicode presentation variants so emoji selectors in legacy
    # translations still use the monochrome outline instead of a system emoji.
    variations = CmapSubtable.newSubtable(14)
    variations.platformID, variations.platEncID, variations.language = 0, 5, 0xFF
    variations.cmap = {}
    variations.uvsDict = {selector: sorted(cmap.items()) for selector in (0xFE0E, 0xFE0F)}
    builder.font["cmap"].tables.append(variations)

    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (1000, getattr(glyph, "xMin", 0)) for name, glyph in glyphs.items()})
    builder.setupHorizontalHeader(ascent=850, descent=-150)
    builder.setupNameTable({
        "familyName": "Mate UI Icons",
        "styleName": "Regular",
        "uniqueFontIdentifier": "Mate UI Icons MDI 7.4.47",
        "fullName": "Mate UI Icons",
        "psName": "MateUIIcons",
        "licenseDescription": "Derived from Material Design Icons under the Apache License 2.0",
        "licenseInfoURL": "https://www.apache.org/licenses/LICENSE-2.0",
    })
    builder.setupOS2(sTypoAscender=850, sTypoDescender=-150, usWinAscent=850, usWinDescent=150)
    builder.setupPost()
    builder.font.flavor = "woff2"
    builder.font.recalcTimestamp = False
    builder.font["head"].created = builder.font["head"].modified = 3786912000
    builder.save(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    build_font(args.package, Path(__file__).resolve().parents[1] / "web/static/mate-ui-icons.woff2")
