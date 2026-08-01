#!/usr/bin/env python3
"""Generate the Crestron `crestron:` domain YAML from a multi-sheet xlsx table.

Usage:
    python3 tools/xlsx_to_yaml.py <input.xlsx> <output_dir_or_yaml_file>
    python3 tools/xlsx_to_yaml.py --check <input.xlsx>

Writes a single `crestron.yaml` holding the port plus all entities grouped by
platform key (light:/switch:/number:/select:/sensor:/cover:). The integration
imports this into a config entry, which lets Home Assistant group an AC's
several entities into one device (device_id/device_name).

The second argument may be either a directory (the file will be named
`crestron.yaml`) or an explicit `.yaml`/`.yml` output path.

Include it in configuration.yaml with:
    crestron: !include crestron.yaml

Pure standard library — no openpyxl/pandas/PyYAML required.
"""

import sys
import os
import argparse
import zipfile
import xml.etree.ElementTree as ET

_A = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class WorkbookValidationError(ValueError):
    """Raised when validation errors make partial YAML unsafe to write."""


class _SheetRows(list):
    """Rows plus the original header cells needed for typo diagnostics."""

    def __init__(self, rows=(), headers=()):
        super().__init__(rows)
        self.headers = tuple(headers)


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
        return _SheetRows()
    header = grid[0]
    rows = []
    for excel_row, raw in enumerate(grid[1:], start=2):
        parsed = {
            header[i]: (raw[i] if i < len(raw) else "")
            for i in range(len(header))
        }
        parsed["_xlsx_row"] = excel_row
        rows.append(parsed)
    return _SheetRows(rows, header)


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


def _add_device_metadata(entity, row, device_id, device_name):
    """Attach stable device identity and a non-authoritative HA area hint."""
    entity["device_id"] = device_id
    entity["device_name"] = device_name
    group = _group(row)
    if group:
        entity["suggested_area"] = group
    entity["_group"] = group


# Home Assistant CoverDeviceClass values supported by cover.py. Keep this list
# in sync with _COVER_DEVICE_CLASSES there; the generator deliberately falls
# back to curtain instead of emitting a value the runtime cannot represent.
SUPPORTED_COVER_TYPES = frozenset({
    "awning",
    "blind",
    "curtain",
    "damper",
    "door",
    "garage",
    "gate",
    "shade",
    "shutter",
    "window",
})


def _cover_type(value):
    """Normalize an xlsx 类型 cell; blank/unknown values become curtain."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_COVER_TYPES else "curtain"


# --------------------------------------------------------------------------- #
# row -> (platform, entity) builders  (pure, unit-tested)
# --------------------------------------------------------------------------- #
def build_light(row):
    """灯光 sheet row -> ('light', entity) or None.

    灯光 sheet 永远只产出 light（绝不产出 switch）。类型严格由 join 能力决定，
    而非「功能」标签：
      - 亮度 + 色温 -> color_temp（双色温）
      - 仅亮度      -> brightness（单色温）
      - 仅开 + 关   -> onoff（只开关）

    混合模拟量与数字量控制、只有色温、开/关不成对或没有任何能力的行都跳过。
    """
    bri = _to_int(row.get("亮度"))
    cct = _to_int(row.get("色温"))
    on = _to_int(row.get("开"))
    off = _to_int(row.get("关"))
    has_analog_control = bri is not None or cct is not None
    has_digital_control = on is not None or off is not None

    if has_analog_control and has_digital_control:
        return None

    if bri is not None:
        name = _name(row, "名称")
        ent = {
            "platform": "crestron",
            "name": name,
            "type": "color_temp" if cct is not None else "brightness",
            "brightness_join": bri,
        }
        if cct is not None:
            ent["color_temp_join"] = cct
        _add_device_metadata(ent, row, f"light_{bri}", name)
        return "light", ent
    if cct is None and on is not None and off is not None:
        name = _name(row, "名称")
        ent = {
            "platform": "crestron",
            "name": name,
            "type": "onoff",
            "on_join": on,
            "off_join": off,
        }
        _add_device_metadata(ent, row, f"light_onoff_{on}", name)
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
    }
    _add_device_metadata(ent, row, f"outlet_{on}", name)
    return "switch", ent


def build_cover(row):
    """窗帘 sheet row -> ('cover', entity) or None.

    开/关/停止为必填的数字量控制 join。「位置」是可选的模拟量 join（0-100，
    0=全关/100=全开）：有则作为 pos_join 输出，让 HA 显示真实百分比；无则
    cover.py 退回假定状态（开/关/停按钮常驻可按，不受反馈影响——符合说明页
    「开和关的控制不能因为反馈状态影响」）。

    「类型」使用 Home Assistant CoverDeviceClass 的英文值；合法值原样输出
    （忽略大小写和首尾空格），空白或未知值一律按 curtain 处理。
    """
    op = _to_int(row.get("开"))
    cl = _to_int(row.get("关"))
    stop = _to_int(row.get("停止"))
    if op is None or cl is None or stop is None:
        return None
    pos = _to_int(row.get("位置"))
    cover_type = _cover_type(row.get("类型"))
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
    _add_device_metadata(ent, row, f"cover_{op}", name)
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
# 空调模式列 -> climate mode_*_join 字段名（控制与反馈共用）
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
    _add_device_metadata(ent, row, f"ac_{on}", base)
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
def dedup_names(entities, on_rename=None):
    """In-place: the 2nd, 3rd... entity sharing a name get ' 2', ' 3' suffixes
    (matches the hand-written configuration.yaml convention)."""
    seen = {}
    for ent in entities:
        base = ent["name"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            renamed = f"{base} {seen[base]}"
            ent["name"] = renamed
            # A one-entity device should receive the same suffix; otherwise HA
            # still shows several devices with an identical device name.
            if ent.get("device_name") == base:
                ent["device_name"] = renamed
            if on_rename is not None:
                on_rename(ent, base, renamed)
    return entities


# --------------------------------------------------------------------------- #
# workbook validation / diagnostics
# --------------------------------------------------------------------------- #
_INSTRUCTION_SHEETS = frozenset({"说明"})
_DIGITAL_COLUMNS = {
    "灯光": ("开", "关"),
    "插座": ("开", "关"),
    "窗帘": ("开", "关", "停止"),
    "空调": (
        "开", "关", "制冷", "制热", "通风", "除湿",
        "低速", "中速", "高速", "自动",
    ),
}
_ANALOG_COLUMNS = {
    "灯光": ("亮度", "色温"),
    "插座": (),
    "窗帘": ("位置",),
    "空调": ("温度", "室温"),
}
_EXPECTED_COLUMNS = {
    "灯光": (
        "序号", "楼层", "房间", "名称", "功能",
        "亮度", "色温", "开", "关",
    ),
    "插座": ("序号", "楼层", "房间", "名称", "开", "关"),
    "窗帘": (
        "序号", "楼层", "房间", "名称", "类型",
        "开", "关", "停止", "位置",
    ),
    "空调": (
        "序号", "楼层", "房间",
        "开", "关", "制冷", "制热", "通风", "除湿",
        "低速", "中速", "高速", "自动", "温度", "室温", "风速值",
    ),
}
_REQUIRED_COLUMNS = {
    "灯光": (),
    "插座": ("开", "关"),
    "窗帘": ("开", "关", "停止"),
    "空调": ("开", "关"),
}


def _header_issues(sheet_name, rows):
    """Return ``(severity, message)`` for every detectable header anomaly.

    Tests and callers may pass an ordinary list of row dictionaries; only rows
    produced by parse_xlsx carry the original headers, so those legacy/pure
    inputs intentionally skip this workbook-level check.
    """
    original = getattr(rows, "headers", None)
    if original is None:
        return []
    headers = [str(value or "").strip() for value in original]
    if not headers:
        return [("error", "sheet is empty or has no header row")]

    issues = []
    nonblank = [header for header in headers if header]
    duplicates = sorted({
        header for header in nonblank if nonblank.count(header) > 1
    })
    if duplicates:
        issues.append(
            ("error", f"duplicate column header(s): {', '.join(duplicates)}")
        )
    if "" in headers:
        positions = [
            str(index + 1) for index, header in enumerate(headers) if not header
        ]
        issues.append(
            (
                "warning",
                "blank column header(s) at column position(s): "
                + ", ".join(positions),
            )
        )

    present = set(nonblank)
    expected = set(_EXPECTED_COLUMNS[sheet_name])
    required = set(_REQUIRED_COLUMNS[sheet_name])
    missing_required = sorted(required - present)
    if missing_required:
        issues.append(
            (
                "error",
                "missing required column header(s): "
                + ", ".join(missing_required),
            )
        )
    missing_optional = sorted((expected - required) - present)
    if missing_optional:
        issues.append(
            (
                "warning",
                "missing recognized column header(s); those fields will not "
                "be converted: " + ", ".join(missing_optional),
            )
        )
    unknown = sorted(present - expected)
    if unknown:
        issues.append(
            (
                "warning",
                "unknown column header(s) will be ignored: "
                + ", ".join(unknown),
            )
        )

    # A light has two alternative control layouts rather than globally
    # required columns. Reject a sheet whose headers cannot express either.
    if sheet_name == "灯光" and (
        "亮度" not in present and not {"开", "关"} <= present
    ):
        issues.append(
            (
                "error",
                "headers cannot express a light control: provide 亮度, "
                "or both 开 and 关",
            )
        )
    return issues


def _join_cell_error(value, maximum):
    """Explain a non-empty invalid join cell; return None when valid/blank."""
    text = str(value or "").strip()
    if not text:
        return None
    if not text.isdigit():
        return "必须只填写十进制数字"
    number = int(text)
    if not 1 <= number <= maximum:
        return f"必须在 1–{maximum} 范围内"
    return None


def _row_errors(sheet_name, row):
    errors = []
    for column in _DIGITAL_COLUMNS[sheet_name]:
        if error := _join_cell_error(row.get(column), 4096):
            errors.append(f"{column}: {error}")
    for column in _ANALOG_COLUMNS[sheet_name]:
        if error := _join_cell_error(row.get(column), 1024):
            errors.append(f"{column}: {error}")
    if errors:
        return errors

    if sheet_name == "灯光":
        brightness = _to_int(row.get("亮度"))
        color_temp = _to_int(row.get("色温"))
        on = _to_int(row.get("开"))
        off = _to_int(row.get("关"))
        if (brightness is not None or color_temp is not None) and (
            on is not None or off is not None
        ):
            errors.append("模拟量控制（亮度/色温）不能与数字量控制（开/关）混填")
        elif color_temp is not None and brightness is None:
            errors.append("填写色温时必须同时填写亮度")
        elif (on is None) != (off is None):
            errors.append("开和关必须成对填写")
        elif brightness is None and on is None:
            errors.append("缺少亮度，或完整的开/关 Join")
    elif sheet_name == "插座":
        if _to_int(row.get("开")) is None or _to_int(row.get("关")) is None:
            errors.append("开和关都是必填 Join")
    elif sheet_name == "窗帘":
        missing = [
            column
            for column in ("开", "关", "停止")
            if _to_int(row.get(column)) is None
        ]
        if missing:
            errors.append(f"缺少必填 Join: {', '.join(missing)}")
    elif sheet_name == "空调":
        if _to_int(row.get("开")) is None or _to_int(row.get("关")) is None:
            errors.append("开和关都是必填 Join")
    return errors


def _placeholder_reason(sheet_name, row):
    """Why a row is intentionally ignored, or None when it needs conversion."""
    relevant = set(_DIGITAL_COLUMNS[sheet_name]) | set(_ANALOG_COLUMNS[sheet_name])
    relevant |= {"楼层", "房间", "名称"}
    values = [str(row.get(column) or "").strip() for column in relevant]
    if not any(values):
        return "empty row"
    if str(row.get("功能") or "").strip() == "//" and not any(
        str(row.get(column) or "").strip()
        for column in set(_DIGITAL_COLUMNS[sheet_name])
        | set(_ANALOG_COLUMNS[sheet_name])
    ):
        return "template row (功能='//' and all Join cells are blank)"
    return None


def _is_placeholder_row(sheet_name, row):
    """Compatibility wrapper used by pure row tests."""
    return _placeholder_reason(sheet_name, row) is not None


def _entity_joins(entity):
    """Yield (signal space, join, field) for duplicate-join warnings."""
    analog_fields = {
        "brightness_join",
        "color_temp_join",
        "pos_join",
        "set_temp_join",
        "reg_temp_join",
    }
    for field, value in entity.items():
        if not field.endswith("_join") or not isinstance(value, int):
            continue
        yield ("analog" if field in analog_fields else "digital", value, field)


def _build_from_workbook(xlsx_path):
    """Parse and validate a workbook, returning entities, counts and messages."""
    sheets = parse_xlsx(xlsx_path)
    by_platform = {}
    errors = 0
    warnings = 0
    seen_joins = {}
    entity_sources = {}
    messages = []

    def report(message):
        messages.append(" ".join(str(message).split()))

    for sheet_name, rows in sheets.items():
        if sheet_name in _INSTRUCTION_SHEETS:
            report(f"[info] ignored instruction sheet {sheet_name!r}")
            continue
        builder = SHEET_BUILDERS.get(sheet_name)
        if builder is None:
            warnings += 1
            report(f"[warning] ignored unknown sheet {sheet_name!r}")
            continue

        for severity, message in _header_issues(sheet_name, rows):
            if severity == "error":
                errors += 1
            else:
                warnings += 1
            report(f"[{severity}] {sheet_name}: {message}")

        placeholders = []
        converted = 0
        for row in rows:
            row_number = row.get("_xlsx_row", "?")
            placeholder_reason = _placeholder_reason(sheet_name, row)
            if placeholder_reason is not None:
                placeholders.append((row_number, placeholder_reason))
                continue
            row_errors = _row_errors(sheet_name, row)
            if row_errors:
                errors += 1
                report(
                    f"[error] {sheet_name}!{row_number}: "
                    + "; ".join(row_errors)
                )
                continue

            if sheet_name == "窗帘":
                raw_type = str(row.get("类型") or "").strip()
                if raw_type and raw_type.lower() not in SUPPORTED_COVER_TYPES:
                    warnings += 1
                    report(
                        f"[warning] {sheet_name}!{row_number}: unknown type "
                        f"{raw_type!r}; using curtain"
                    )
            if sheet_name == "空调":
                fan_value = str(row.get("风速值") or "").strip()
                if fan_value:
                    report(
                        f"[info] {sheet_name}!{row_number}: 风速值 "
                        f"{fan_value!r} is intentionally ignored; digital "
                        "低速/中速/高速/自动 Join columns are used"
                    )

            result = builder(row)
            if not result:
                errors += 1
                report(
                    f"[error] {sheet_name}!{row_number}: "
                    "row could not be converted"
                )
                continue
            if isinstance(result, tuple):
                result = [result]
            for platform, entity in result:
                entity_sources[id(entity)] = (sheet_name, row_number)
                by_platform.setdefault(platform, []).append(entity)
                converted += 1
                for space, join, field in _entity_joins(entity):
                    key = (space, join)
                    owner = seen_joins.get(key)
                    current = f"{sheet_name}!{row_number} {entity['name']} ({field})"
                    if owner is not None:
                        warnings += 1
                        report(
                            f"[warning] duplicate {space} join {join}: "
                            f"{owner}; {current}"
                        )
                    else:
                        seen_joins[key] = current
        if placeholders:
            details = ", ".join(
                f"{row_number} ({reason})"
                for row_number, reason in placeholders
            )
            report(
                f"[info] {sheet_name}: ignored {len(placeholders)} "
                f"empty/template row(s): {details}"
            )
        report(
            f"[info] {sheet_name}: converted {converted} entity row(s)"
        )

    def renamed(entity, old_name, new_name):
        nonlocal warnings
        warnings += 1
        source_sheet, source_row = entity_sources.get(id(entity), ("?", "?"))
        report(
            f"[warning] {source_sheet}!{source_row}: "
            f"duplicate entity name {old_name!r}; "
            f"renamed to {new_name!r}"
        )

    for entities in by_platform.values():
        dedup_names(entities, renamed)
    return by_platform, errors, warnings, messages


# --------------------------------------------------------------------------- #
# YAML emitting (hand-rolled) — one `crestron.yaml` domain config file
# --------------------------------------------------------------------------- #
# Stable, readable order for the platform sections under `crestron:`.
_PLATFORM_ORDER = ["light", "switch", "cover", "number", "select",
                   "sensor", "binary_sensor", "media_player", "climate"]


# Control characters that have a YAML double-quoted escape. Anything else in
# the C0 range is emitted as \xNN.
_YAML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


# YAML 1.1 reads these bare words as booleans or null, so a string that happens
# to equal one has to be quoted or it changes type on load.
_YAML_RESERVED = frozenset(
    "y yes n no true false on off null none ~".split()
)


def _is_safe_bare(s):
    """True when ``s`` survives a YAML round-trip unquoted, as a string.

    Requiring a leading letter (or underscore) is what keeps numbers out:
    ``123`` would load as an int and ``0x1F`` as 31, silently retyping a name.
    """
    if not s or not s.isascii():
        return False
    if not all(c.isalnum() or c == "_" for c in s):
        return False
    if not (s[0].isalpha() or s[0] == "_"):
        return False
    return s.lower() not in _YAML_RESERVED


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    # Leave plain ASCII identifiers bare (brightness/curtain/...), double-quote
    # anything else (names with spaces, dots, Chinese, reserved words, digits).
    if _is_safe_bare(s):
        return s
    # A raw newline inside a double-quoted scalar folds into a space (or breaks
    # the document); an Excel cell with alt-enter in it produced exactly that.
    out = []
    for ch in s:
        escape = _YAML_ESCAPES.get(ch)
        if escape is not None:
            out.append(escape)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


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


def emit_domain(by_platform, source, report_lines=()):
    """by_platform: {platform: [entities]} -> the `crestron:` domain config.

    Entities live under their platform key (light:/switch:/number:/...) so they
    are set up via the integration's config entry and grouped into HA devices.
    """
    lines = [
        f"# Generated from {source} by tools/xlsx_to_yaml.py",
        "# Include in configuration.yaml as:  crestron: !include crestron.yaml",
    ]
    if report_lines:
        lines.extend(["#", "# Conversion report (same messages printed by the tool):"])
        for message in report_lines:
            physical_lines = str(message).replace("\r", "\n").split("\n")
            lines.extend(f"# {line}" if line else "#" for line in physical_lines)
    lines.extend(
        [
            "",
            "port: 10200  # <- 改成你的快思聪 XSIG 端口；如有 to_joins/from_joins 也放这里",
            "",
        ]
    )
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
                # A newline in a 楼层/房间 cell would end the comment and let
                # the rest of the text land in the document as YAML.
                lines.append("  # " + " ".join(str(group).split()))
                last_group = group
            # Drop the legacy `platform:` key — under the domain config the
            # platform is implied by the section key.
            lines.extend(_emit_entity(ent, 2, skip=("_group", "platform")))
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _resolve_output_path(output):
    """Return (parent_directory, yaml_path) for either supported CLI form."""
    if output.lower().endswith((".yaml", ".yml")):
        return os.path.dirname(output) or ".", output
    return output, os.path.join(output, "crestron.yaml")


def generate(xlsx_path, output):
    by_platform, errors, warnings, messages = _build_from_workbook(xlsx_path)
    for message in messages:
        print("  " + message)
    source = os.path.basename(xlsx_path)
    if errors:
        raise WorkbookValidationError(
            f"refusing to write partial YAML: {errors} workbook error(s)"
        )

    out_dir, path = _resolve_output_path(output)
    counts = ", ".join(f"{p}:{len(e)}" for p, e in by_platform.items())
    summary = (
        f"wrote {path} ({counts}; errors:{errors}, warnings:{warnings})"
    )
    usage = _usage_text(path)
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            emit_domain(
                by_platform,
                source,
                messages + [summary, "", usage],
            )
        )
    print("  " + summary)
    print("\n" + usage)
    return by_platform


def check(xlsx_path):
    """Validate without writing YAML; return a process exit status."""
    by_platform, errors, warnings, messages = _build_from_workbook(xlsx_path)
    for message in messages:
        print("  " + message)
    counts = ", ".join(f"{p}:{len(e)}" for p, e in by_platform.items())
    print(
        f"  check complete ({counts}; errors:{errors}, warnings:{warnings})"
    )
    return 1 if errors else 0


def _usage_text(path):
    """Return the post-generation instructions printed and embedded in YAML."""
    filename = os.path.basename(path)
    return (
        "下一步（如何使用这个配置文件）：\n"
        f"  1. 把生成的 {filename} 复制到 Home Assistant 配置目录\n"
        "     （与 configuration.yaml 同一个文件夹）。\n"
        "  2. 在 configuration.yaml 里加一行（include 这个文件）：\n"
        f"         crestron: !include {filename}\n"
        "     注意：整个 configuration.yaml 只能有一个 `crestron:` 键。端口写在\n"
        f"     {filename} 顶部的 `port:`，不要在 configuration.yaml 里再写一个\n"
        "     `crestron:`（会冲突）。\n"
        f"  3. 打开 {filename}，把顶部的 `port:` 改成你的快思聪 XSIG 端口。\n"
        "  4. 重启 Home Assistant。\n"
        "  5. 若实体类型变过（如旧的开关灯变成灯、空调合并成一个 climate），重启后到\n"
        "     设置 → 设备与服务 → 实体，筛选「不可用」，把旧实体批量删除即可。\n"
    )


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the xlsx and print diagnostics without writing YAML",
    )
    parser.add_argument("xlsx", help="input .xlsx workbook")
    parser.add_argument(
        "output",
        nargs="?",
        help="output directory or .yaml file (not used with --check)",
    )
    args = parser.parse_args(argv[1:])
    if args.check:
        if args.output is not None:
            parser.error("output must be omitted with --check")
        return check(args.xlsx)
    if args.output is None:
        parser.error("output is required unless --check is used")
    try:
        generate(args.xlsx, args.output)
    except WorkbookValidationError as error:
        print(f"  [aborted] {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
