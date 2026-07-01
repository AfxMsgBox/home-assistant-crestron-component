#!/usr/bin/env python3
"""Generate the Crestron `crestron:` domain YAML from a multi-sheet xlsx table.

Usage:
    python3 tools/xlsx_to_yaml.py <input.xlsx> <output_dir>

Writes a single `crestron.yaml` holding the port plus all entities grouped by
platform key (light:/switch:/number:/select:/sensor:/cover:). The integration
imports this into a config entry, which lets Home Assistant group an AC's
several entities into one device (device_id/device_name).

Include it in configuration.yaml with:
    crestron: !include crestron.yaml

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
def build_light(row):
    """灯光 sheet row -> ('light', entity) or None.

    灯光 sheet 永远只产出 light（绝不产出 switch）。能力由「哪一列带 join」决定，
    而非「功能」标签：有亮度 join -> 调光灯（色温可选）；只有开/关 -> 只开关的灯
    （relay 式、无亮度，ColorMode.ONOFF——只开关的灯仍是灯）；都没有 -> 跳过
    （如 '//' 占位）。
    """
    bri = _to_int(row.get("亮度"))
    cct = _to_int(row.get("色温"))
    on = _to_int(row.get("开"))
    off = _to_int(row.get("关"))
    if bri is not None:
        name = _name(row, "名称")
        ent = {
            "platform": "crestron",
            "name": name,
            "type": "brightness",
            "brightness_join": bri,
        }
        if cct is not None:
            ent["color_temp_join"] = cct
        ent["device_id"] = f"light_{bri}"
        ent["device_name"] = name
        ent["_group"] = _group(row)
        return "light", ent
    if on is not None and off is not None:
        name = _name(row, "名称")
        ent = {
            "platform": "crestron",
            "name": name,
            "on_join": on,
            "off_join": off,
            "device_id": f"light_onoff_{on}",
            "device_name": name,
            "_group": _group(row),
        }
        return "light", ent
    return None


def build_outlet(row):
    """插座 sheet row -> ('switch', entity) or None.

    插座用两根数字量 join：开/关。快思聪侧会把状态也回传到这两根
    join 上（开=1/关=0 表示通电，开=0/关=1 表示断电），switch 平台负责
    订阅并判定状态。
    """
    on = _to_int(row.get("开"))
    off = _to_int(row.get("关"))
    if on is None or off is None:
        return None
    name = _name(row, "名称")
    ent = {
        "platform": "crestron",
        "name": name,
        "on_join": on,
        "off_join": off,
        "device_class": "outlet",
        "device_id": f"outlet_{on}",
        "device_name": name,
        "_group": _group(row),
    }
    return "switch", ent


def build_cover(row):
    """窗帘 sheet row -> ('cover', entity) or None.

    开/关/停止为必填的数字量控制 join。「位置」是可选的模拟量 join（0-100，
    0=全关/100=全开）：有则作为 pos_join 输出，让 HA 显示真实百分比；无则
    cover.py 退回假定状态（开/关/停按钮常驻可按，不受反馈影响——符合说明页
    「开和关的控制不能因为反馈状态影响」）。
    """
    op = _to_int(row.get("开"))
    cl = _to_int(row.get("关"))
    stop = _to_int(row.get("停止"))
    if op is None or cl is None or stop is None:
        return None
    pos = _to_int(row.get("位置"))
    label = row.get("名称", "").strip()
    cover_type = "shade" if label.startswith("卷") else "curtain"
    name = _name(row, "名称")
    ent = {
        "platform": "crestron",
        "name": name,
        "type": cover_type,
        "open_join": op,
        "close_join": cl,
        "stop_join": stop,
    }
    if pos is not None:
        ent["pos_join"] = pos
    ent["device_id"] = f"cover_{op}"
    ent["device_name"] = name
    ent["_group"] = _group(row)
    return "cover", ent


_AC_MODE_COLS = ("制冷", "制热", "通风", "除湿")
_AC_FAN_COLS = ("低速", "中速", "高速", "自动")


def _join_map(row, cols):
    """{col_label: join} for the columns that carry a join."""
    out = {}
    for col in cols:
        v = _to_int(row.get(col))
        if v is not None:
            out[col] = v
    return out


# 空调风速列 -> climate fan_*_join 字段名
_AC_FAN_JOIN_KEYS = {
    "低速": "fan_low_join",
    "中速": "fan_med_join",
    "高速": "fan_high_join",
    "自动": "fan_auto_join",
}
# 空调模式列 -> climate mode_*_join 字段名(只读，仅用于显示运行模式)
_AC_MODE_JOIN_KEYS = {
    "制冷": "mode_cool_join",
    "制热": "mode_heat_join",
    "通风": "mode_fan_join",
    "除湿": "mode_dry_join",
}


def build_ac(row):
    """空调 sheet row -> ('climate', entity) or None.

    One climate entity per AC = one device, one thermostat card, and it shows
    up in HA's "空调"(climate) category. Power is a real on/off button (pulsed
    on/off joins); temperature setpoint and fan speed are settable; the running
    mode (制冷/制热/通风/除湿) is a real selectable mode that tracks the control
    system's feedback joins. Needs power joins; without them the AC can't be a
    climate.

    「风速值」列（模拟量风速 -1/10/50/100）暂不使用：数字量风速（低/中/高/自动）
    已能完整表达风速，两路并存会让状态互相打架。故此处只读数字量风速列，
    模拟量「风速值」有意忽略。
    """
    on = _to_int(row.get("开"))
    off = _to_int(row.get("关"))
    if on is None or off is None:
        return None
    base = _name(row, None) + " 空调"
    set_temp = _to_int(row.get("温度"))
    room_temp = _to_int(row.get("室温"))
    modes = _join_map(row, _AC_MODE_COLS)
    fans = _join_map(row, _AC_FAN_COLS)

    ent = {"platform": "crestron", "name": base, "on_join": on, "off_join": off}
    if set_temp is not None:
        ent["set_temp_join"] = set_temp
    if room_temp is not None:
        ent["reg_temp_join"] = room_temp
    for label, join in modes.items():
        ent[_AC_MODE_JOIN_KEYS[label]] = join
    for label, join in fans.items():
        ent[_AC_FAN_JOIN_KEYS[label]] = join
    ent["device_id"] = f"ac_{on}"
    ent["device_name"] = base
    ent["_group"] = _group(row)
    return "climate", ent


SHEET_BUILDERS = {
    "灯光": build_light,
    "插座": build_outlet,
    "窗帘": build_cover,
    "空调": build_ac,
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
# YAML emitting (hand-rolled) — one `crestron.yaml` domain config file
# --------------------------------------------------------------------------- #
# Stable, readable order for the platform sections under `crestron:`.
_PLATFORM_ORDER = ["light", "switch", "cover", "number", "select",
                   "sensor", "binary_sensor", "media_player", "climate"]


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    # Leave plain ASCII identifiers bare (brightness/curtain/...), double-quote
    # anything else (names with spaces, dots, Chinese, etc.).
    if s and all(c.isalnum() or c == "_" for c in s) and s.isascii():
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _emit_entity(ent, indent, skip=("_group",)):
    """Render one entity as YAML list-item lines; '- ' bullet at `indent` cols."""
    pad = " " * indent
    lines = []
    first = True
    for key, value in ent.items():
        if key in skip:
            continue
        bullet = "- " if first else "  "
        if isinstance(value, dict):
            lines.append(f"{pad}{bullet}{key}:")
            for k, v in value.items():
                lines.append(f"{pad}    {_yaml_scalar(k)}: {_yaml_scalar(v)}")
        else:
            lines.append(f"{pad}{bullet}{key}: {_yaml_scalar(value)}")
        first = False
    return lines


def emit_domain(by_platform, source):
    """by_platform: {platform: [entities]} -> the `crestron:` domain config.

    Entities live under their platform key (light:/switch:/number:/...) so they
    are set up via the integration's config entry and grouped into HA devices.
    """
    lines = [
        f"# Generated from {source} by tools/xlsx_to_yaml.py",
        "# Include in configuration.yaml as:  crestron: !include crestron.yaml",
        "",
        "port: 10200  # <- 改成你的快思聪 XSIG 端口；如有 to_joins/from_joins 也放这里",
        "",
    ]
    ordered = sorted(
        by_platform,
        key=lambda p: (_PLATFORM_ORDER.index(p) if p in _PLATFORM_ORDER
                       else len(_PLATFORM_ORDER), p),
    )
    for platform in ordered:
        lines.append(f"{platform}:")
        last_group = None
        for ent in by_platform[platform]:
            group = ent.get("_group")
            if group and group != last_group:
                lines.append(f"  # {group}")
                last_group = group
            # Drop the legacy `platform:` key — under the domain config the
            # platform is implied by the section key.
            lines.extend(_emit_entity(ent, 2, skip=("_group", "platform")))
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def generate(xlsx_path, out_dir):
    sheets = parse_xlsx(xlsx_path)
    source = os.path.basename(xlsx_path)
    by_platform = {}  # platform -> [entities]  (in sheet order)

    for sheet_name, rows in sheets.items():
        builder = SHEET_BUILDERS.get(sheet_name)
        if builder is None:
            print(f"  [skip] unknown sheet {sheet_name!r}")
            continue
        skipped = 0
        for row in rows:
            result = builder(row)
            if not result:
                skipped += 1
                continue
            if isinstance(result, tuple):
                result = [result]  # single-entity builders
            for platform, ent in result:
                by_platform.setdefault(platform, []).append(ent)
        if skipped:
            print(f"  [{sheet_name}] skipped {skipped} placeholder/empty row(s)")

    for entities in by_platform.values():
        dedup_names(entities)

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "crestron.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(emit_domain(by_platform, source))
    counts = ", ".join(f"{p}:{len(e)}" for p, e in by_platform.items())
    print(f"  wrote {path}  ({counts})")
    _print_usage(path)
    return by_platform


def _print_usage(path):
    """Tell the user exactly what to do with the file we just wrote."""
    print(
        "\n"
        "下一步（如何使用这个配置文件）：\n"
        f"  1. 把生成的 {os.path.basename(path)} 复制到 Home Assistant 配置目录\n"
        "     （与 configuration.yaml 同一个文件夹）。\n"
        "  2. 在 configuration.yaml 里加一行（include 这个文件）：\n"
        "         crestron: !include crestron.yaml\n"
        "     注意：整个 configuration.yaml 只能有一个 `crestron:` 键。端口写在\n"
        "     crestron.yaml 顶部的 `port:`，不要在 configuration.yaml 里再写一个\n"
        "     `crestron:`（会冲突）。\n"
        f"  3. 打开 {os.path.basename(path)}，把顶部的 `port:` 改成你的快思聪 XSIG 端口。\n"
        "  4. 重启 Home Assistant。\n"
        "  5. 若实体类型变过（如旧的开关灯变成灯、空调合并成一个 climate），重启后到\n"
        "     设置 → 设备与服务 → 实体，筛选「不可用」，把旧实体批量删除即可。\n"
    )


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    generate(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
