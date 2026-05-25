"""Load Aseprite's bundled GPL palettes for database seeding."""

from __future__ import annotations

import json
import re
from pathlib import Path

ASEPRITE_PALETTES_DIR = Path(__file__).parent / "data" / "palettes" / "aseprite"

FOLDER_LABELS: dict[str, str] = {
    "adigunpolack-palettes": "Adigun Polack",
    "arne-palettes": "Arne",
    "davitmasia-palettes": "Davit Masia",
    "dawnbringer-palettes": "DawnBringer",
    "endesga-palettes": "Endesga",
    "hardware-palettes": "Hardware",
    "hyohnoo-palettes": "Hyohnoo",
    "javierguerrero-palettes": "Javier Guerrero",
    "pico8-palette": "PICO-8",
    "pinetreepizza-palettes": "PineTreePizza",
    "software-palettes": "Software",
    "zughy-palettes": "Zughy",
}

# Aseprite's default new-sprite palette — seed last so it sorts first (created_at DESC).
PRIORITY_LAST = {"db32.gpl"}

_RGB_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)(?:\s+\d+)?(?:\s|$)")


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _title_from_stem(stem: str) -> str:
    return stem.replace("-", " ").replace("_", " ").upper()


def parse_gpl(path: Path) -> tuple[str, list[str]]:
    """Parse a GIMP palette file into (display_name, hex_colors)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    display_name = _title_from_stem(path.stem)
    colors: list[str] = []

    for line in text.splitlines():
        if line.startswith("Name:"):
            display_name = line.split(":", 1)[1].strip()
            continue
        match = _RGB_LINE.match(line)
        if not match:
            continue
        r, g, b = (int(match.group(i)) for i in range(1, 4))
        colors.append(_rgb_to_hex(r, g, b))

    parent = path.parent.name if path.parent != ASEPRITE_PALETTES_DIR else ""
    label = FOLDER_LABELS.get(parent, "Aseprite")
    if path.name == "tags.gpl":
        name = "Tags"
    elif parent:
        name = f"{display_name} ({label})"
    else:
        name = display_name

    return name, colors


def load_aseprite_palettes() -> list[tuple[str, list[str]]]:
    if not ASEPRITE_PALETTES_DIR.exists():
        return []

    paths = sorted(ASEPRITE_PALETTES_DIR.rglob("*.gpl"), key=lambda p: (p.name in PRIORITY_LAST, p.as_posix()))
    palettes: list[tuple[str, list[str]]] = []
    seen_names: set[str] = set()

    for path in paths:
        name, colors = parse_gpl(path)
        if not colors:
            continue
        unique = name
        suffix = 2
        while unique in seen_names:
            unique = f"{name} #{suffix}"
            suffix += 1
        seen_names.add(unique)
        palettes.append((unique, colors))

    return palettes


def seed_aseprite_palettes(conn) -> int:
    """Insert bundled Aseprite palettes that are not already present. Returns count inserted."""
    existing = {row[0] for row in conn.execute("SELECT name FROM palettes").fetchall()}
    inserted = 0

    for name, colors in load_aseprite_palettes():
        if name in existing:
            continue
        conn.execute(
            "INSERT INTO palettes (name, colors) VALUES (?, ?)",
            (name, json.dumps(colors)),
        )
        existing.add(name)
        inserted += 1

    return inserted
