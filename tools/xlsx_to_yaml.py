#!/usr/bin/env python3
"""将多工作表 Excel 转换为 Crestron 设备 YAML。

用法：
    python3 tools/xlsx_to_yaml.py 输入.xlsx 输出目录或文件.yaml
    python3 tools/xlsx_to_yaml.py --check 输入.xlsx

每行生成一台设备，支持灯光、插座、窗帘和空调，保留设备标识及楼层、房间。
输出路径为目录时，文件名固定为 crestron.yaml；也可指定 .yaml/.yml 文件。
在 configuration.yaml 中引入：
    crestron: !include crestron.yaml

仅使用 Python 标准库，无需安装额外依赖。
"""

from collections import Counter
import sys
import os
import argparse
import zipfile
import xml.etree.ElementTree as ET

_A = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class WorkbookValidationError(ValueError):
    """工作簿校验失败时中止生成，避免写出不完整配置。"""


class _SheetRows(list):
    """保存设备行和原始表头，用于检查缺列、重名及拼写错误。"""

    def __init__(self, rows=(), headers=()):
        super().__init__(rows)
        self.headers = tuple(headers)


# --------------------------------------------------------------------------- #
# 读取 Excel（仅使用标准库）
# --------------------------------------------------------------------------- #
def _col_index(ref):
    """把单元格引用转换为从 0 开始的列号，例如 B7 对应 1。"""
    letters = "".join(c for c in ref if c.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def parse_xlsx(path):
    """返回“工作表名 → 行字典列表”。第一行作表头，单元格文字去除首尾空格。"""
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
    excel_row = 0
    for row in data.findall(_A + "row"):
        # XML 可能省略空行，需保留实际 Excel 行号，避免错误位置提前。
        # 缺少 r 属性时，按上一行号加 1 处理。
        excel_row = int(row.get("r", excel_row + 1))
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
        grid.append((excel_row, [cells.get(i, "") for i in range(maxc + 1)]))
    if not grid:
        return _SheetRows()
    _, header = grid[0]
    rows = []
    for excel_row, raw in grid[1:]:
        parsed = {
            header[i]: (raw[i] if i < len(raw) else "")
            for i in range(len(header))
        }
        parsed["_xlsx_row"] = excel_row
        rows.append(parsed)
    return _SheetRows(rows, header)


# --------------------------------------------------------------------------- #
# 通用辅助函数
# --------------------------------------------------------------------------- #
def _to_int(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _name(row, name_col):
    label = row.get(name_col, "").strip() if name_col else ""
    return label


def _group(row):
    floor = row.get("楼层", "").strip()
    room = row.get("房间", "").strip()
    return ".".join(p for p in (floor, room) if p)


def _add_device_metadata(entity, row, device_id, device_name):
    """附加稳定的设备标识及 Excel 原始楼层、房间。"""
    entity["device_id"] = device_id
    entity["device_name"] = device_name
    entity["floor"] = row.get("楼层", "").strip()
    entity["room"] = row.get("房间", "").strip()
    entity["_group"] = _group(row)


# 与 cover.py 的 _COVER_DEVICE_CLASSES 保持一致。
# 无法识别的类型回退为 curtain，避免生成运行时不支持的配置。
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
    """规范化“类型”列；空白或未知值使用 curtain（布帘）。"""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_COVER_TYPES else "curtain"


# --------------------------------------------------------------------------- #
# 将设备行转换为平台和实体配置（纯函数）
# --------------------------------------------------------------------------- #
def build_light(row):
    """将灯光行转换为 light 配置；能力组合无效时返回 None。

    灯光工作表生成 light 实体，类型由信号能力决定，
    而非「功能」标签：
      - 亮度 + 色温 -> color_temp（双色温）
      - 仅亮度      -> brightness（单色温）
      - 仅开 + 关   -> onoff（只开关）

    混合控制、只有色温、开关不成对或无控制能力时返回 None；完整转换入口会报错并中止生成。
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
    """将插座行转换为 switch 配置；缺少开关编号时返回 None。

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
    """将窗帘行转换为 cover 配置；缺少开、关、停止编号时返回 None。

    开/关/停止为必填的数字量控制 join。「位置」是可选的模拟量 join（0-100，
    0=全关/100=全开）：有则输出 pos_join，收到反馈后显示真实百分比；
    未收到位置反馈时使用假定状态。开、关、停止按钮不受当前反馈状态限制。

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
    """将已填写编号的列整理成“列名 → 信号编号”字典。"""
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
    """将空调行转换为 climate 配置；缺少开关编号时返回 None。

    每台空调对应一个实体，设温、模式和风速能力由已填写的信号编号决定。
    风速只使用低速、中速、高速、自动四列的数字量编号，暂不读取模拟量“风速值”。
    """
    on = _to_int(row.get("开"))
    off = _to_int(row.get("关"))
    if on is None or off is None:
        return None
    base = _name(row, "名称")
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
# 同房间名称去重（纯函数）
# --------------------------------------------------------------------------- #
def dedup_names(entities, on_rename=None):
    """跨平台为同房间的重名设备编号。

    先保留“吊灯1”等原始唯一名称，自动编号避开冲突；相同位置的设备共用房间。
    """
    def key(ent):
        return (ent.get("floor", "").casefold().replace(" ", ""),
                ent.get("room", "").casefold().replace(" ", ""))

    counts = Counter((key(ent), ent["name"].casefold()) for ent in entities)
    used = {}
    for ent in entities:
        group, name = key(ent), ent["name"].casefold()
        if counts[group, name] == 1:
            used.setdefault(group, set()).add(name)
    for ent in entities:
        group, base = key(ent), ent["name"]
        if counts[group, base.casefold()] == 1:
            continue
        occupied = used.setdefault(group, set())
        number = 1
        while (renamed := f"{base}{number}").casefold() in occupied:
            number += 1
        occupied.add(renamed.casefold())
        ent["name"] = renamed
        if ent.get("device_name") == base:
            ent["device_name"] = renamed
        if on_rename is not None:
            on_rename(ent, base, renamed)
    return entities


# --------------------------------------------------------------------------- #
# 工作簿校验与提示
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
        "序号", "楼层", "房间", "名称",
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
    """返回表头问题的“级别、说明”列表。

    仅 parse_xlsx 的结果含原始表头；直接传入普通行列表时跳过表头检查。
    """
    original = getattr(rows, "headers", None)
    if original is None:
        return []
    headers = [str(value or "").strip() for value in original]
    if not headers:
        return [("error", "工作表为空或缺少表头，请在第一行填写列名")]

    issues = []
    nonblank = [header for header in headers if header]
    duplicates = sorted({
        header for header in nonblank if nonblank.count(header) > 1
    })
    if duplicates:
        issues.append(
            ("error", f"表头重名，请修改重复列名：{', '.join(duplicates)}")
        )
    if "" in headers:
        positions = [
            str(index + 1) for index, header in enumerate(headers) if not header
        ]
        issues.append(
            (
                "warning",
                "以下列的表头为空，请检查列名："
                + ", ".join(positions),
            )
        )

    present = set(nonblank)
    expected = set(_EXPECTED_COLUMNS[sheet_name])
    required = set(_REQUIRED_COLUMNS[sheet_name]) | {"楼层", "房间", "名称"}
    missing_required = sorted(required - present)
    if missing_required:
        issues.append(
            (
                "error",
                "缺少必填表头："
                + ", ".join(missing_required),
            )
        )
    missing_optional = sorted((expected - required) - present)
    if missing_optional:
        issues.append(
            (
                "warning",
                "缺少以下可选表头，对应字段不转换：" + ", ".join(missing_optional),
            )
        )
    # 关联列由独立的自动化生成器处理，不写入设备配置。
    unknown = sorted(present - expected - {"HA关联开关实体ID", "HA关联人在实体ID"})
    if unknown:
        issues.append(
            (
                "warning",
                "以下表头无法识别，对应列已忽略："
                + ", ".join(unknown),
            )
        )

    # 灯光支持亮度控制或成对开关控制，表头至少需满足其中一种。
    if sheet_name == "灯光" and (
        "亮度" not in present and not {"开", "关"} <= present
    ):
        issues.append(
            (
                "error",
                "缺少灯光控制表头：请提供亮度列，或同时提供开、关两列",
            )
        )
    return issues


def _join_cell_error(value, maximum):
    """说明非空信号编号的格式或范围错误；合法值和空白返回 None。"""
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
    errors = [f"{column}: 必填，不能为空" for column in ("楼层", "房间", "名称")
              if not str(row.get(column) or "").strip()]
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
            errors.append("缺少亮度编号，或完整的开/关编号")
    elif sheet_name == "插座":
        if _to_int(row.get("开")) is None or _to_int(row.get("关")) is None:
            errors.append("开和关的信号编号均须填写")
    elif sheet_name == "窗帘":
        missing = [
            column
            for column in ("开", "关", "停止")
            if _to_int(row.get(column)) is None
        ]
        if missing:
            errors.append(f"缺少必填信号编号: {', '.join(missing)}")
    elif sheet_name == "空调":
        if _to_int(row.get("开")) is None or _to_int(row.get("关")) is None:
            errors.append("开和关的信号编号均须填写")
    return errors


def _placeholder_reason(sheet_name, row):
    """返回空行或模板行的忽略原因；需要转换的设备行返回 None。"""
    relevant = set(_DIGITAL_COLUMNS[sheet_name]) | set(_ANALOG_COLUMNS[sheet_name])
    relevant |= {"楼层", "房间", "名称"}
    values = [str(row.get(column) or "").strip() for column in relevant]
    if not any(values):
        return "空行"
    if str(row.get("功能") or "").strip() == "//" and not any(
        str(row.get(column) or "").strip()
        for column in set(_DIGITAL_COLUMNS[sheet_name])
        | set(_ANALOG_COLUMNS[sheet_name])
    ):
        return "模板行（功能为 //，所有信号编号为空）"
    return None


def _is_placeholder_row(sheet_name, row):
    """兼容直接测试设备行的调用方式。"""
    return _placeholder_reason(sheet_name, row) is not None


def _entity_joins(entity):
    """依次返回信号类型、编号、字段名，用于检查重复信号编号。"""
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
    """解析并校验工作簿，返回实体、错误数、警告数及提示。"""
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
            report(f"[信息] 已忽略说明工作表：{sheet_name}")
            continue
        builder = SHEET_BUILDERS.get(sheet_name)
        if builder is None:
            warnings += 1
            report(f"[警告] 工作表“{sheet_name}”无法识别，已忽略；支持灯光、插座、窗帘、空调")
            continue

        for severity, message in _header_issues(sheet_name, rows):
            if severity == "error":
                errors += 1
            else:
                warnings += 1
            level = "错误" if severity == "error" else "警告"
            report(f"[{level}] {sheet_name}表：{message}")

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
                    f"[错误] {sheet_name}表第{row_number}行："
                    + "；".join(row_errors)
                )
                continue

            if sheet_name == "窗帘":
                raw_type = str(row.get("类型") or "").strip()
                if raw_type and raw_type.lower() not in SUPPORTED_COVER_TYPES:
                    warnings += 1
                    report(
                        f"[警告] {sheet_name}表第{row_number}行：类型“{raw_type}”无法识别，"
                        "已使用 curtain（布帘）"
                    )
            if sheet_name == "空调":
                fan_value = str(row.get("风速值") or "").strip()
                if fan_value:
                    message = (
                        f"[信息] {sheet_name}表第{row_number}行：“风速值”编号{fan_value}未使用，"
                        "风速改用低速/中速/高速/自动列的编号。"
                    )
                    if not _join_map(row, _AC_FAN_COLS):
                        message += "这些列均为空，未配置风速调节。"
                    report(message)

            result = builder(row)
            if not result:
                errors += 1
                report(
                    f"[错误] {sheet_name}表第{row_number}行：无法转换，请检查必填项和控制信号"
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
                    current = f"{sheet_name}表第{row_number}行 {entity['name']} ({field})"
                    if owner is not None:
                        warnings += 1
                        report(
                            f"[警告] {'模拟量' if space == 'analog' else '数字量'}信号编号{join}重复："
                            f"{owner}；{current}。请核对是否共用信号"
                        )
                    else:
                        seen_joins[key] = current
        if placeholders:
            details = ", ".join(
                f"{row_number} ({reason})"
                for row_number, reason in placeholders
            )
            report(
                f"[信息] {sheet_name}表：已忽略{len(placeholders)}个空行或模板行，行号：{details}"
            )
        report(
            f"[信息] {sheet_name}表：已转换{converted}条设备配置"
        )

    def renamed(entity, old_name, new_name):
        nonlocal warnings
        warnings += 1
        source_sheet, source_row = entity_sources.get(id(entity), ("?", "?"))
        report(
            f"[警告] {source_sheet}表第{source_row}行：同房间设备名“{old_name}”重复，"
            f"已改名为“{new_name}”"
        )

    dedup_names([entity for entities in by_platform.values() for entity in entities], renamed)
    return by_platform, errors, warnings, messages


# --------------------------------------------------------------------------- #
# 生成单个 crestron.yaml 配置文件
# --------------------------------------------------------------------------- #
# 固定平台输出顺序，便于阅读和比较配置。
_PLATFORM_ORDER = ["light", "switch", "cover", "number", "select",
                   "sensor", "binary_sensor", "media_player", "climate"]
_PLATFORM_LABELS = {
    "light": "灯光", "switch": "开关", "cover": "窗帘", "number": "数值设置",
    "select": "选项", "sensor": "传感器", "binary_sensor": "二值传感器",
    "media_player": "媒体播放器", "climate": "空调",
}


# 双引号字符串中的常用转义；其他 C0 控制字符输出为 \xNN。
_YAML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


# YAML 1.1 会将这些裸词解析成布尔值或空值，作为名称时必须加引号。
_YAML_RESERVED = frozenset(
    "y yes n no true false on off null none ~".split()
)


def _is_safe_bare(s):
    """判断字符串不加引号时，能否在 YAML 回读后保持字符串类型。

    仅接受字母或下划线开头的标识，避免 123、0x1F 等名称被解析成数字。
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
    # 普通 ASCII 标识可不加引号；中文、空格、点号、保留词和数字名称使用双引号。
    if _is_safe_bare(s):
        return s
    # 转义单元格内换行，避免 YAML 将其折叠为空格或破坏配置结构。
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
    """把单个实体转换为 YAML 列表项，按 indent 缩进。"""
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
    """把按平台分组的实体写成 crestron 域配置。

    实体放在 light、switch 等平台键下，由集成加载并关联到 HA 设备。
    """
    lines = [
        f"# 来源：{source}；由 tools/xlsx_to_yaml.py 生成",
        "# 在 configuration.yaml 中引入：crestron: !include crestron.yaml",
    ]
    if report_lines:
        lines.extend(["#", "# 转换报告（与终端提示一致）："])
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
                # 合并楼层/房间中的换行，避免注释后的文字变成有效 YAML。
                lines.append("  # " + " ".join(str(group).split()))
                last_group = group
            # 平台已由外层键确定，不再写入旧式 platform 字段。
            lines.extend(_emit_entity(ent, 2, skip=("_group", "platform")))
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def _resolve_output_path(output):
    """将输出目录或文件参数统一解析为父目录和 YAML 文件路径。"""
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
            f"工作簿有{errors}项错误，已中止生成，原 YAML 文件保持不变"
        )

    out_dir, path = _resolve_output_path(output)
    counts = "，".join(f"{_PLATFORM_LABELS.get(p, p)}：{len(e)}" for p, e in by_platform.items())
    summary = (
        f"已生成 {path}（{counts}；错误：{errors}，警告：{warnings}）"
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
    """只检查、不写 YAML；返回进程退出码。"""
    by_platform, errors, warnings, messages = _build_from_workbook(xlsx_path)
    for message in messages:
        print("  " + message)
    counts = "，".join(f"{_PLATFORM_LABELS.get(p, p)}：{len(e)}" for p, e in by_platform.items())
    print(
        f"  检查完成（{counts}；错误：{errors}，警告：{warnings}）；未写入文件"
    )
    return 1 if errors else 0


def _usage_text(path):
    """生成终端与 YAML 注释中共用的后续操作说明。"""
    filename = os.path.basename(path)
    return (
        "下一步（如何使用这个配置文件）：\n"
        f"  1. 把生成的 {filename} 复制到 Home Assistant 配置目录\n"
        "     （与 configuration.yaml 同一个文件夹）。\n"
        "  2. 在 configuration.yaml 中引入（只保留一个 crestron 配置项）：\n"
        f"         crestron: !include {filename}\n"
        f"  3. 打开 {filename}，把顶部的 `port:` 改成你的快思聪 XSIG 端口。\n"
        "  4. 仅修改 YAML 可执行 crestron.reload；首次安装或更新组件代码需重启 HA。\n"
        "  5. 如需清理旧实体，请先筛选 Crestron 集成，确认实体已从配置移除且不再使用。\n"
    )


class _ChineseArgumentParser(argparse.ArgumentParser):
    """将帮助标题及本工具会触发的参数错误改为中文。"""

    def format_usage(self):
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self):
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message):
        message = message.replace("the following arguments are required: ", "缺少必填参数：")
        message = message.replace("unrecognized arguments: ", "无法识别的参数：")
        message = message.replace("ignored explicit argument", "此选项不接受附加值")
        message = message.replace("argument ", "参数 ")
        self.print_usage(sys.stderr)
        self.exit(2, f"错误：{message}\n")


def main(argv):
    parser = _ChineseArgumentParser(
        description=__doc__, add_help=False, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "可选参数"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 Excel 并显示结果，不生成 YAML",
    )
    parser.add_argument("xlsx", metavar="输入.xlsx", help="输入的 .xlsx 设备表")
    parser.add_argument(
        "output",
        nargs="?",
        metavar="输出路径",
        help="输出目录或 .yaml 文件；使用 --check 时省略",
    )
    args = parser.parse_args(argv[1:])
    if args.check:
        if args.output is not None:
            parser.error("使用 --check 时不能指定输出路径")
        return check(args.xlsx)
    if args.output is None:
        parser.error("请指定输出路径，或使用 --check 仅检查")
    try:
        generate(args.xlsx, args.output)
    except WorkbookValidationError as error:
        print(f"  [中止] {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
