from __future__ import annotations

from datetime import datetime, timedelta
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import zipfile


MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

BUILTIN_NUMBER_FORMATS = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    14: "mm-dd-yy",
    49: "@",
}

INDEXED_COLOURS = [
    "000000", "FFFFFF", "FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF",
    "000000", "FFFFFF", "FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF",
    "800000", "008000", "000080", "808000", "800080", "008080", "C0C0C0", "808080",
    "9999FF", "993366", "FFFFCC", "CCFFFF", "660066", "FF8080", "0066CC", "CCCCFF",
    "000080", "FF00FF", "FFFF00", "00FFFF", "800080", "800000", "008080", "0000FF",
    "00CCFF", "CCFFFF", "CCFFCC", "FFFF99", "99CCFF", "FF99CC", "CC99FF", "FFCC99",
    "3366FF", "33CCCC", "99CC00", "FFCC00", "FF9900", "FF6600", "666699", "969696",
    "003366", "339966", "003300", "333300", "993300", "993366", "333399", "333333",
]


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters.upper():
        value = value * 26 + ord(letter) - 64
    return value


def _column_letters(number: int) -> str:
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _cell_parts(reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", reference.upper())
    if not match:
        raise ValueError(f"Invalid cell reference: {reference}")
    return int(match.group(2)), _column_number(match.group(1))


def _range_parts(reference: str) -> tuple[int, int, int, int]:
    start, _, end = reference.partition(":")
    end = end or start
    start_row, start_column = _cell_parts(start)
    end_row, end_column = _cell_parts(end)
    return start_row, start_column, end_row, end_column


def _normalise_sheet_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(text.text or "" for text in item.iter(f"{MAIN}t"))
        for item in root.findall(f"{MAIN}si")
    ]


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(f"{PKG_REL}Relationship")
    }
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall(f"{MAIN}sheets/{MAIN}sheet"):
        target = targets.get(sheet.attrib.get(f"{REL}id", ""), "")
        if target and not target.startswith("xl/"):
            target = f"xl/{target.lstrip('/')}"
        result.append((sheet.attrib.get("name", "Sheet"), target))
    return result


def _theme_colours(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/theme/theme1.xml"))
    except KeyError:
        return []
    scheme = root.find(f".//{DRAWING}clrScheme")
    if scheme is None:
        return []
    by_name: dict[str, str] = {}
    for child in list(scheme):
        colour = next(iter(child), None)
        if colour is None:
            by_name[child.tag.rsplit("}", 1)[-1]] = ""
        else:
            by_name[child.tag.rsplit("}", 1)[-1]] = (
                colour.attrib.get("lastClr") or colour.attrib.get("val") or ""
            )
    # SpreadsheetML theme indices use this semantic order, which differs
    # from the physical dk1/lt1 order in theme1.xml.
    order = ["lt1", "dk1", "lt2", "dk2", *[f"accent{index}" for index in range(1, 7)], "hlink", "folHlink"]
    return [by_name.get(name, "") for name in order]


def _tint_colour(hex_colour: str, tint: float) -> str:
    if not hex_colour or tint == 0:
        return hex_colour
    channels = [int(hex_colour[index:index + 2], 16) for index in (0, 2, 4)]
    if tint < 0:
        channels = [round(channel * (1 + tint)) for channel in channels]
    else:
        channels = [round(channel + (255 - channel) * tint) for channel in channels]
    return "".join(f"{max(0, min(255, channel)):02X}" for channel in channels)


def _colour(element: ET.Element | None, theme_colours: list[str]) -> str | None:
    if element is None:
        return None
    value = element.attrib.get("rgb", "")
    if value:
        value = value[-6:]
    elif "theme" in element.attrib:
        index = int(element.attrib["theme"])
        value = theme_colours[index] if index < len(theme_colours) else ""
    elif "indexed" in element.attrib:
        index = int(element.attrib["indexed"])
        value = INDEXED_COLOURS[index] if index < len(INDEXED_COLOURS) else ""
    if not value:
        return None
    return f"#{_tint_colour(value, float(element.attrib.get('tint', '0')))}"


def _styles(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return [{}]
    themes = _theme_colours(archive)
    number_formats = dict(BUILTIN_NUMBER_FORMATS)
    for number_format in root.findall(f"{MAIN}numFmts/{MAIN}numFmt"):
        number_formats[int(number_format.attrib["numFmtId"])] = number_format.attrib.get("formatCode", "")

    fonts: list[dict[str, Any]] = []
    for font in root.findall(f"{MAIN}fonts/{MAIN}font"):
        underline = font.find(f"{MAIN}u")
        name = font.find(f"{MAIN}name")
        size = font.find(f"{MAIN}sz")
        fonts.append({
            "name": name.attrib.get("val", "Arial") if name is not None else "Arial",
            "size": float(size.attrib.get("val", "10")) if size is not None else 10.0,
            "bold": font.find(f"{MAIN}b") is not None,
            "italic": font.find(f"{MAIN}i") is not None,
            "underline": underline.attrib.get("val", "single") if underline is not None else None,
            "colour": _colour(font.find(f"{MAIN}color"), themes),
        })

    fills: list[dict[str, Any]] = []
    for fill in root.findall(f"{MAIN}fills/{MAIN}fill"):
        pattern = fill.find(f"{MAIN}patternFill")
        fills.append({
            "pattern": pattern.attrib.get("patternType") if pattern is not None else None,
            "foreground": _colour(pattern.find(f"{MAIN}fgColor"), themes) if pattern is not None else None,
            "background": _colour(pattern.find(f"{MAIN}bgColor"), themes) if pattern is not None else None,
        })

    borders: list[dict[str, Any]] = []
    for border in root.findall(f"{MAIN}borders/{MAIN}border"):
        sides: dict[str, Any] = {}
        for name in ("left", "right", "top", "bottom"):
            side = border.find(f"{MAIN}{name}")
            sides[name] = {
                "style": side.attrib.get("style") if side is not None else None,
                "colour": _colour(side.find(f"{MAIN}color"), themes) if side is not None else None,
            }
        borders.append(sides)

    result: list[dict[str, Any]] = []
    cell_formats = root.findall(f"{MAIN}cellXfs/{MAIN}xf")
    for cell_format in cell_formats:
        alignment = cell_format.find(f"{MAIN}alignment")
        font_id = int(cell_format.attrib.get("fontId", "0"))
        fill_id = int(cell_format.attrib.get("fillId", "0"))
        border_id = int(cell_format.attrib.get("borderId", "0"))
        number_format_id = int(cell_format.attrib.get("numFmtId", "0"))
        result.append({
            "font": fonts[font_id] if font_id < len(fonts) else {},
            "fill": fills[fill_id] if fill_id < len(fills) else {},
            "border": borders[border_id] if border_id < len(borders) else {},
            "alignment": {
                "horizontal": alignment.attrib.get("horizontal") if alignment is not None else None,
                "vertical": alignment.attrib.get("vertical") if alignment is not None else None,
                "wrap": alignment is not None and alignment.attrib.get("wrapText") == "1",
                "shrink": alignment is not None and alignment.attrib.get("shrinkToFit") == "1",
                "indent": int(alignment.attrib.get("indent", "0")) if alignment is not None else 0,
                "rotation": int(alignment.attrib.get("textRotation", "0")) if alignment is not None else 0,
            },
            "number_format": number_formats.get(number_format_id, "General"),
        })
    return result or [{}]


def _raw_cell_value(cell: ET.Element, shared_strings: list[str]) -> tuple[Any, str | None]:
    cell_type = cell.attrib.get("t")
    formula = cell.find(f"{MAIN}f")
    value = cell.find(f"{MAIN}v")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{MAIN}t")), formula.text if formula is not None else None
    if value is None:
        return "", formula.text if formula is not None else None
    raw = value.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)], formula.text if formula is not None else None
        except (ValueError, IndexError):
            return raw, formula.text if formula is not None else None
    if cell_type == "b":
        return raw == "1", formula.text if formula is not None else None
    if cell_type in {"str", "e"}:
        return raw, formula.text if formula is not None else None
    try:
        return float(raw), formula.text if formula is not None else None
    except ValueError:
        return raw, formula.text if formula is not None else None


def _display_value(value: Any, number_format: str) -> str:
    if value == "":
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if not isinstance(value, (int, float)):
        return str(value)
    cleaned = re.sub(r'"[^"]*"', "", number_format or "General")
    if "%" in cleaned:
        decimals = len(re.search(r"\.(0+)", cleaned).group(1)) if re.search(r"\.(0+)", cleaned) else 0
        return f"{value * 100:.{decimals}f}%"
    if re.search(r"[dmyhs]", cleaned, re.I):
        try:
            date = datetime(1899, 12, 30) + timedelta(days=float(value))
            return date.strftime("%d/%m/%Y")
        except (OverflowError, ValueError):
            pass
    if cleaned in {"0", "#,##0"}:
        return f"{value:,.0f}" if "," in cleaned else f"{value:.0f}"
    if cleaned in {"0.00", "#,##0.00"}:
        return f"{value:,.2f}" if "," in cleaned else f"{value:.2f}"
    return str(int(value)) if float(value).is_integer() else str(value)


def _sheet_hint_candidates(template_id: str, template_code: str, title: str) -> list[str]:
    title_head, _, title_tail = title.partition(" ")
    candidates = [template_code, title_head, title_tail]
    if template_code == "PERIMETER_OF_CONSOLIDATION":
        candidates.append("77")
    local_id = template_id.split(":", 2)[-1]
    candidates.append(local_id)
    _, separator, local_suffix = local_id.partition("_")
    if separator and local_suffix:
        candidates.append(local_suffix)
    prefixes = [template_code, template_id.split(":")[1] if ":" in template_id else ""]
    for prefix in prefixes:
        normalised_prefix = re.sub(r"[^A-Z0-9]", "", (prefix or "").upper())
        if normalised_prefix and _normalise_sheet_name(local_id).startswith(normalised_prefix):
            remainder = re.sub(
                rf"^{re.escape(prefix)}[_\s-]*",
                "",
                local_id,
                flags=re.I,
            ).strip("_ ")
            if remainder:
                candidates.append(remainder)
    for value in (template_code, title_head, local_id):
        code_match = re.search(r"(?:^|[^A-Z])C?(\d{1,3})(?:[._](\d+))?", value or "", re.I)
        if not code_match:
            continue
        major = str(int(code_match.group(1)))
        minor = code_match.group(2)
        candidates.append(
            f"{major}.{int(minor)}"
            if minor and int(minor)
            else major
        )
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _select_sheet(
    sheets: list[tuple[str, str]],
    *,
    template_id: str,
    template_code: str,
    title: str,
) -> tuple[str, str] | None:
    if not sheets:
        return None
    by_name = {_normalise_sheet_name(name): (name, target) for name, target in sheets}
    for candidate in _sheet_hint_candidates(template_id, template_code, title):
        match = by_name.get(_normalise_sheet_name(candidate))
        if match:
            return match
    normalised_title = _normalise_sheet_name(title)
    title_matches = [
        (name, target)
        for name, target in sheets
        if _normalise_sheet_name(name)
        and normalised_title.startswith(_normalise_sheet_name(name))
    ]
    if title_matches:
        return max(title_matches, key=lambda item: len(_normalise_sheet_name(item[0])))
    if len(sheets) == 1:
        return sheets[0]
    return None


def parse_xlsx_layout(
    path: Path,
    *,
    template_id: str,
    template_code: str,
    title: str,
) -> dict[str, Any] | None:
    """Return a browser-renderable worksheet preserving the source XLSX layout."""
    if not path.exists() or path.suffix.lower() not in {".xlsx", ".xlsm", ".xltx"}:
        return None
    with zipfile.ZipFile(path) as archive:
        sheets = _sheet_targets(archive)
        selected = _select_sheet(
            sheets,
            template_id=template_id,
            template_code=template_code,
            title=title,
        )
        if selected is None:
            return None
        sheet_name, target = selected
        if target not in archive.namelist():
            return None
        root = ET.fromstring(archive.read(target))
        shared = _shared_strings(archive)
        styles = _styles(archive)

        dimension = root.find(f"{MAIN}dimension")
        dimension_ref = dimension.attrib.get("ref", "A1:A1") if dimension is not None else "A1:A1"
        start_row, start_column, end_row, end_column = _range_parts(dimension_ref)

        row_elements = {
            int(row.attrib["r"]): row
            for row in root.findall(f"{MAIN}sheetData/{MAIN}row")
        }
        cell_elements: dict[tuple[int, int], ET.Element] = {}
        for row_number, row in row_elements.items():
            for cell in row.findall(f"{MAIN}c"):
                cell_row, cell_column = _cell_parts(cell.attrib["r"])
                cell_elements[(cell_row, cell_column)] = cell
        if end_row - start_row > 50_000 or end_column - start_column > 250:
            meaningful_positions = [
                position
                for position, element in cell_elements.items()
                if any(value not in {"", None} for value in _raw_cell_value(element, shared))
            ]
            if not meaningful_positions:
                return None
            used_rows = [position[0] for position in meaningful_positions]
            used_columns = [position[1] for position in meaningful_positions]
            if end_column - start_column > 250:
                start_column, end_column = min(used_columns), max(used_columns)
            dimension_ref = (
                f"{_column_letters(start_column)}{start_row}:"
                f"{_column_letters(end_column)}{end_row}"
            )
        if end_row - start_row > 50_000 or end_column - start_column > 250:
            return None
        sparse = end_row - start_row > 2_000

        sheet_format = root.find(f"{MAIN}sheetFormatPr")
        default_column_width = float(sheet_format.attrib.get("defaultColWidth", "8.43")) if sheet_format is not None else 8.43
        default_row_height = float(sheet_format.attrib.get("defaultRowHeight", "15")) if sheet_format is not None else 15.0

        column_specs: dict[int, dict[str, Any]] = {}
        for column in root.findall(f"{MAIN}cols/{MAIN}col"):
            for index in range(int(column.attrib["min"]), int(column.attrib["max"]) + 1):
                if start_column <= index <= end_column:
                    column_specs[index] = {
                        "width": float(column.attrib.get("width", default_column_width)),
                        "hidden": column.attrib.get("hidden") == "1",
                        "style_id": int(column.attrib.get("style", "0")),
                    }

        merges: dict[tuple[int, int], dict[str, int]] = {}
        covered: set[tuple[int, int]] = set()
        merge_refs: list[str] = []
        merged_ranges: list[dict[str, int]] = []
        for merge in root.findall(f"{MAIN}mergeCells/{MAIN}mergeCell"):
            reference = merge.attrib["ref"]
            merge_refs.append(reference)
            merge_start_row, merge_start_column, merge_end_row, merge_end_column = _range_parts(reference)
            merged_ranges.append({
                "start_row": merge_start_row,
                "start_column": merge_start_column,
                "end_row": merge_end_row,
                "end_column": merge_end_column,
            })
            merges[(merge_start_row, merge_start_column)] = {
                "row_span": merge_end_row - merge_start_row + 1,
                "column_span": merge_end_column - merge_start_column + 1,
            }
            for row_number in range(merge_start_row, merge_end_row + 1):
                for column_number in range(merge_start_column, merge_end_column + 1):
                    if (row_number, column_number) != (merge_start_row, merge_start_column):
                        covered.add((row_number, column_number))

        columns: list[dict[str, Any]] = []
        for column_number in range(start_column, end_column + 1):
            spec = column_specs.get(column_number, {})
            columns.append({
                "index": column_number,
                "letter": _column_letters(column_number),
                "width": spec.get("width", default_column_width),
                "hidden": spec.get("hidden", False),
                "style_id": spec.get("style_id", 0),
                "reporting_code": None,
            })

        rows: list[dict[str, Any]] = []
        for row_number in range(start_row, end_row + 1):
            row_element = row_elements.get(row_number)
            row_style = int(row_element.attrib.get("s", "0")) if row_element is not None else 0
            row_data = {
                "index": row_number,
                "height": float(row_element.attrib.get("ht", default_row_height)) if row_element is not None else default_row_height,
                "hidden": row_element is not None and row_element.attrib.get("hidden") == "1",
                "style_id": row_style,
                "reporting_code": None,
                "cells": [],
            }
            if sparse and row_element is not None:
                row_columns = sorted({
                    _cell_parts(element.attrib["r"])[1]
                    for element in row_element.findall(f"{MAIN}c")
                    if start_column <= _cell_parts(element.attrib["r"])[1] <= end_column
                })
            elif sparse:
                row_columns = []
            else:
                row_columns = range(start_column, end_column + 1)
            for column_number in row_columns:
                if (row_number, column_number) in covered:
                    continue
                element = cell_elements.get((row_number, column_number))
                column_style = column_specs.get(column_number, {}).get("style_id", 0)
                style_id = int(element.attrib.get("s", row_style or column_style)) if element is not None else row_style or column_style
                value, formula = _raw_cell_value(element, shared) if element is not None else ("", None)
                number_format = styles[style_id].get("number_format", "General") if style_id < len(styles) else "General"
                row_data["cells"].append({
                    "reference": f"{_column_letters(column_number)}{row_number}",
                    "column": column_number,
                    "style_id": style_id,
                    "value": _display_value(value, number_format),
                    "raw_value": value,
                    "formula": formula,
                    **merges.get((row_number, column_number), {}),
                })
            rows.append(row_data)

        # Regulatory workbooks conventionally put coordinate codes on a single
        # header row. Detect that row and the corresponding row-code column so
        # exact workbook cells remain connected to existing datapoint records.
        header_row: dict[str, Any] | None = None
        for row in rows[:30]:
            numeric_cells = [
                cell for cell in row["cells"]
                if re.fullmatch(r"\d{3,5}", str(cell["value"]).strip())
            ]
            if len(numeric_cells) >= 3:
                header_row = row
                for cell in numeric_cells:
                    for column in columns:
                        if column["index"] == cell["column"]:
                            column["reporting_code"] = str(cell["value"]).strip()
                            break
                break
        if header_row is not None:
            coordinate_columns = [column["index"] for column in columns if column["reporting_code"]]
            first_coordinate_column = min(coordinate_columns) if coordinate_columns else end_column + 1
            for row in rows:
                if row["index"] <= header_row["index"]:
                    continue
                for cell in row["cells"]:
                    value = str(cell["value"]).strip()
                    if cell["column"] < first_coordinate_column and re.fullmatch(r"\d{3,5}", value):
                        row["reporting_code"] = value
                        break

        sheet_view = root.find(f"{MAIN}sheetViews/{MAIN}sheetView")
        pane = sheet_view.find(f"{MAIN}pane") if sheet_view is not None else None
        return {
            "sheet_name": sheet_name,
            "dimension": dimension_ref,
            "range": {
                "start_row": start_row,
                "start_column": start_column,
                "end_row": end_row,
                "end_column": end_column,
            },
            "default_row_height": default_row_height,
            "default_column_width": default_column_width,
            "zoom": int(sheet_view.attrib.get("zoomScaleNormal", "100")) if sheet_view is not None else 100,
            "freeze": {
                "rows": int(float(pane.attrib.get("ySplit", "0"))) if pane is not None else 0,
                "columns": int(float(pane.attrib.get("xSplit", "0"))) if pane is not None else 0,
            },
            "merge_refs": merge_refs,
            "merged_ranges": merged_ranges,
            "sparse": sparse,
            "styles": styles,
            "columns": columns,
            "rows": rows,
        }
