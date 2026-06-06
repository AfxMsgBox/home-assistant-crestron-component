#!/usr/bin/env python3
"""Generate per-platform Crestron YAML from a multi-sheet xlsx join table.

Usage:
    python3 tools/xlsx_to_yaml.py <input.xlsx> <output_dir>

The workbook has one sheet per device category:
  - 灯光  -> light.yaml + switch.yaml   (split by which columns carry a join)
  - 空调  -> climate.yaml
  - 窗帘  -> cover.yaml

Each output file is a YAML *list* of entities, ready to be pulled into
configuration.yaml with e.g.  ``light: !include light.yaml``.

Pure standard library — no openpyxl/pandas/PyYAML required.
"""

import sys
import os
import zipfile
import xml.etree.ElementTree as ET

_A = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# --------------------------------------------------------------------------- #
# xlsx parsing (stdlib only)
# --------------------------------------------------------------------------- #
def _col_index(ref):
    """'B7' -> 1 (zero-based column index)."""
    letters = "".join(c for c in ref if c.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def parse_xlsx(path):
    """Return {sheet_name: list_of_row_dicts}. Row dicts are keyed by the
    header (first row) cell text; values are stripped strings."""
    z = zipfile.ZipFile(path)

    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in sst.findall(_A + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_A + "t")))

    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_target = {r.get("Id"): r.get("Target") for r in rels}
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

    sheets = {}
    for s in wb.find(_A + "sheets", ):
        name = s.get("name")
        target = rid_target[s.get(R)]
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets[name] = _read_sheet(z, target, shared)
    return sheets


def _read_sheet(z, path, shared):
    ws = ET.fromstring(z.read(path))
    data = ws.find(_A + "sheetData")
    grid = []
    for row in data.findall(_A + "row"):
        cells = {}
        maxc = -1
        for c in row.findall(_A + "c"):
            ci = _col_index(c.get("r"))
            maxc = max(maxc, ci)
            t = c.get("t")
            v = c.find(_A + "v")
            val = ""
            if v is not None:
                val = v.text or ""
                if t == "s":
                    val = shared[int(val)]
            else:
                isv = c.find(_A + "is")
                if isv is not None:
                    val = "".join(tt.text or "" for tt in isv.iter(_A + "t"))
            cells[ci] = val.strip()
        grid.append([cells.get(i, "") for i in range(maxc + 1)])
    if not grid:
        return []
    header = grid[0]
    rows = []
    for raw in grid[1:]:
        rows.append({header[i]: (raw[i] if i < len(raw) else "")
                     for i in range(len(header))})
    return rows


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _to_int(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _name(row, name_col):
    floor = row.get("楼层", "").strip()
    room = row.get("房间", "").strip()
    label = row.get(name_col, "").strip() if name_col else ""
    prefix = ".".join(p for p in (floor, room) if p)
    return f"{prefix} {label}".strip() if label else prefix


def _group(row):
    floor = row.get("楼层", "").strip()
    room = row.get("房间", "").strip()
    return ".".join(p for p in (floor, room) if p)


# --------------------------------------------------------------------------- #
# row -> (platform, entity) builders  (pure, unit-tested)
# --------------------------------------------------------------------------- #
def build_light_or_switch(row):
    """灯光 sheet row -> ('light'|'switch', entity) or None.

    Capability is read from which columns carry a join, not from the 功能
    label: a brightness join -> dimmable light (color_temp optional); an
    on/off pair -> relay switch; neither -> skip (e.g. the '//' placeholder).
    """
    bri = _to_int(row.get("亮度"))
    cct = _to_int(row.get("色温"))
    on = _to_int(row.get("开"))
    off = _to_int(row.get("关"))
    if bri is not None:
        ent = {
            "platform": "crestron",
            "name": _name(row, "名字"),
            "type": "brightness",
            "brightness_join": bri,
        }
        if cct is not None:
            ent["color_temp_join"] = cct
        ent["_group"] = _group(row)
        return "light", ent
    if on is not None and off is not None:
        ent = {
            "platform": "crestron",
            "name": _name(row, "名字"),
            "on_join": on,
            "off_join": off,
            "_group": _group(row),
        }
        return "switch", ent
    return None


def build_cover(row):
    """窗帘 sheet row -> ('cover', entity) or None."""
    op = _to_int(row.get("开"))
    cl = _to_int(row.get("关"))
    stop = _to_int(row.get("停止"))
    if op is None or cl is None or stop is None:
        return None
    label = row.get("名称", "").strip()
    cover_type = "shade" if label.startswith("卷") else "curtain"
    return "cover", {
        "platform": "crestron",
        "name": _name(row, "名称"),
        "type": cover_type,
        "open_join": op,
        "close_join": cl,
        "stop_join": stop,
        "_group": _group(row),
    }


# 空调 column -> climate yaml key (only emitted when the cell has a join)
_CLIMATE_MAP = [
    ("开", "on_join"),
    ("关", "off_join"),
    ("制冷", "mode_cool_join"),
    ("制热", "mode_heat_join"),
    ("通风", "mode_fan_join"),
    ("除湿", "mode_dry_join"),
    ("低速", "fan_low_join"),
    ("中速", "fan_med_join"),
    ("高速", "fan_high_join"),
    ("自动", "fan_auto_join"),
    ("温度", "set_temp_join"),
    ("室温", "reg_temp_join"),
]


def build_climate(row):
    """空调 sheet row -> ('climate', entity) or None."""
    ent = {"platform": "crestron", "name": _name(row, None) + " 空调"}
    found = False
    for col, key in _CLIMATE_MAP:
        v = _to_int(row.get(col))
        if v is not None:
            ent[key] = v
            found = True
    if not found:
        return None
    ent["_group"] = _group(row)
    return "climate", ent


SHEET_BUILDERS = {
    "灯光": build_light_or_switch,
    "窗帘": build_cover,
    "空调": build_climate,
}


# --------------------------------------------------------------------------- #
# name de-duplication  (pure)
# --------------------------------------------------------------------------- #
def dedup_names(entities):
    """In-place: the 2nd, 3rd... entity sharing a name get ' 2', ' 3' suffixes
    (matches the hand-written configuration.yaml convention)."""
    seen = {}
    for ent in entities:
        base = ent["name"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            ent["name"] = f"{base} {seen[base]}"
    return entities


# --------------------------------------------------------------------------- #
# YAML emitting (hand-rolled; entities are flat scalars)
# --------------------------------------------------------------------------- #
def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    # Leave plain ASCII identifiers bare (crestron/brightness/curtain/...),
    # double-quote anything else (names with spaces, dots, Chinese, etc.).
    if s and all(c.isalnum() or c == "_" for c in s) and s.isascii():
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def emit_yaml(entities, source, platform):
    lines = [
        f"# Generated from {source} by tools/xlsx_to_yaml.py",
        f"# Include in configuration.yaml as:  {platform}: !include {platform}.yaml",
        "",
    ]
    last_group = None
    for ent in entities:
        group = ent.get("_group")
        if group and group != last_group:
            lines.append(f"# {group}")
            last_group = group
        first = True
        for key, value in ent.items():
            if key == "_group":
                continue
            prefix = "- " if first else "  "
            lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
            first = False
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def generate(xlsx_path, out_dir):
    sheets = parse_xlsx(xlsx_path)
    by_platform = {}  # platform -> list of entities (in sheet order)
    source = os.path.basename(xlsx_path)

    for sheet_name, rows in sheets.items():
        builder = SHEET_BUILDERS.get(sheet_name)
        if builder is None:
            print(f"  [skip] unknown sheet {sheet_name!r}")
            continue
        skipped = 0
        for row in rows:
            result = builder(row)
            if result is None:
                skipped += 1
                continue
            platform, ent = result
            by_platform.setdefault(platform, []).append(ent)
        if skipped:
            print(f"  [{sheet_name}] skipped {skipped} placeholder/empty row(s)")

    os.makedirs(out_dir, exist_ok=True)
    for platform, entities in sorted(by_platform.items()):
        before = [e["name"] for e in entities]
        dedup_names(entities)
        renamed = sum(1 for a, e in zip(before, entities) if a != e["name"])
        path = os.path.join(out_dir, f"{platform}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(emit_yaml(entities, source, platform))
        note = f" ({renamed} renamed for dup names)" if renamed else ""
        print(f"  wrote {path}: {len(entities)} entities{note}")
    return by_platform


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    generate(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
