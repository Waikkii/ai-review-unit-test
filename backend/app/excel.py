from io import BytesIO
from zipfile import ZipFile, BadZipFile
import re
from copy import copy
from dataclasses import dataclass
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from .schemas import COLUMNS
from .config import settings

@dataclass
class DetailLayout:
    top: int
    bottom: int
    shared: dict
    groups: list
    start: int
    end: int


def detail_header(sheet):
    """识别第一轮下完整的四列组，不能把其他轮次或 AI 列当成输入。"""
    sections = [cell.row for row in sheet.iter_rows() for cell in row
                if cell.value == "测试详情"]
    if len(sections) > 1:
        raise ValueError(f"{sheet.title}: 存在多个测试详情区域，暂不支持")
    start = sections[0] + 1 if sections else 1
    end = min(sheet.max_row, start + 29)
    for top in range(start, end + 1):
        candidates = [(top, top + 2)] if sections else [(top, top)]
        for first, bottom in candidates:
            if bottom > end:
                continue
            positions = {name: set() for name in ["测试对象", *COLUMNS[:7]]}
            for row in sheet.iter_rows(min_row=first, max_row=bottom):
                for cell in row:
                    name = str(cell.value).strip() if cell.value is not None else ""
                    if name in positions:
                        positions[name].add(cell.column)
            if all(len(positions[name]) == 1 for name in COLUMNS[:7]) and len(positions["测试对象"]) <= 1:
                columns = {name: next(iter(cols)) for name, cols in positions.items() if cols}
                if sections:
                    parents = ["测试对象", *COLUMNS[:7]]
                    if "测试对象" not in columns:
                        continue
                    if not all(sheet.cell(first, columns[name]).value == name and any(
                        area.min_row == first and area.max_row == bottom
                        and area.min_col == columns[name] == area.max_col
                        for area in sheet.merged_cells.ranges) for name in parents):
                        continue
                    rounds = [area for area in sheet.merged_cells.ranges
                              if area.min_row == first and area.max_row == bottom - 1
                              and sheet.cell(first, area.min_col).value == "第一轮"]
                    if len(rounds) != 1:
                        continue
                    left, right = rounds[0].min_col, rounds[0].max_col
                    # 允许右侧存在有自己分组标题的其他轮次 / AI 审查；无归属的重名列拒绝猜测。
                    other_headers = [area for area in sheet.merged_cells.ranges
                                     if area.min_row == first and area.max_row == bottom - 1
                                     and area.min_col > right and sheet.cell(first, area.min_col).value]
                    if any(cell.value in COLUMNS[7:] and not any(a.min_col <= cell.column <= a.max_col for a in other_headers)
                           for cell in sheet[bottom] if cell.column > right):
                        continue
                else:
                    repeated = [cell.column for cell in sheet[bottom] if cell.value in COLUMNS[7:]]
                    if not repeated:
                        continue
                    left, right = min(repeated), max(repeated)
                if right < max(columns.values()) or (right - left + 1) % 4:
                    continue
                groups = [dict(zip(COLUMNS[7:], range(col, col + 4))) for col in range(left, right + 1, 4)]
                if not all(sheet.cell(bottom, col).value == name for group in groups for name, col in group.items()):
                    continue
                return DetailLayout(first, bottom, columns, groups, left, right)
    raise ValueError(f"{sheet.title}: 未找到完整、唯一的测试详情三行表头，请检查合并单元格和字段名")


def parse_excel(content):
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > 150 * 1024 * 1024:
                raise ValueError("Excel 解压后过大")
        workbook = load_workbook(BytesIO(content), data_only=False, keep_links=False)
    except (BadZipFile, OSError, KeyError) as exc:
        raise ValueError("无法读取有效的 .xlsx 文件") from exc
    parsed = []
    try:
        for sheet in workbook:
            if sheet.max_row * sheet.max_column > 2_000_000:
                raise ValueError(f"{sheet.title} 工作表范围过大")
            if not any(c.value is not None for row in sheet.iter_rows(max_row=min(sheet.max_row, 30)) for c in row):
                continue
            if sheet.title.strip() in {"封面", "目录"}:
                continue
            layout = detail_header(sheet)
            hrow = layout.bottom
            merged = {}
            for area in sheet.merged_cells.ranges:
                # 横向合并是说明/分组行，不将“/”扩散为实测值。
                if area.min_row <= hrow or area.min_col != area.max_col:
                    continue
                top = sheet.cell(area.min_row, area.min_col)
                for row in range(area.min_row, area.max_row + 1):
                    for col in range(area.min_col, area.max_col + 1):
                        merged[row, col] = top
            bands = {(area.min_row, area.min_col): sheet.cell(area.min_row, area.min_col).value
                     for area in sheet.merged_cells.ranges if area.min_row == area.max_row > hrow
                     and area.max_col - area.min_col == 3}
            group_labels = ["" for _ in layout.groups]
            for row in range(hrow + 1, sheet.max_row + 1):
                for index, group in enumerate(layout.groups):
                    start = group["门限"]
                    if (row, start) in bands:
                        group_labels[index] = str(bands[row, start] or "")
                    columns = {**layout.shared, **group}
                    value_cell = merged.get((row, columns["值"]), sheet.cell(row, columns["值"]))
                    if value_cell.value is None or not str(value_cell.value).strip():
                        continue
                    values = {}
                    for name, col in columns.items():
                        cell = merged.get((row, col), sheet.cell(row, col))
                        value = cell.value
                        if name == "附件链接" and cell.hyperlink and cell.hyperlink.target:
                            value = cell.hyperlink.target
                        elif cell.data_type == "f":
                            # HYPERLINK 常量可读取；其他公式拒绝猜测缓存值。
                            match = re.fullmatch(r'(?i)=HYPERLINK\(\s*"([^"]+)"\s*[,;]\s*"[^"]*"\s*\)', str(value))
                            if name == "附件链接" and match:
                                value = match.group(1)
                            else:
                                raise ValueError(f"{sheet.title} 第 {row} 行 第 {index + 1} 组 {name}: 请将公式转成值后上传")
                        values[name] = "" if value is None else str(value)
                    label = group_labels[index] or f"第{index + 1}组" if len(layout.groups) > 1 else ""
                    parsed.append({"domain": sheet.title, "row_number": row, "fields": values,
                                   "source_group": index, "source_label": label})
                    if len(parsed) > settings.max_items:
                        raise ValueError("待审查项超过配置上限")
    finally:
        workbook.close()
    if not parsed:
        raise ValueError("未发现测试详情中“值”非空的测试项")
    return parsed

def safe_cell(value):
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value

def _insert_columns(sheet, at, count):
    """保留右侧已有区域的单元格、合并区和列宽。"""
    if at > sheet.max_column:
        return
    merges = [area for area in sheet.merged_cells.ranges if area.max_col >= at]
    dimensions = [(key, copy(value)) for key, value in sheet.column_dimensions.items()]
    for area in merges:
        sheet.unmerge_cells(str(area))
    sheet.insert_cols(at, count)
    for area in merges:
        left = area.min_col + (count if area.min_col >= at else 0)
        right = area.max_col + (count if area.max_col >= at else 0)
        sheet.merge_cells(start_row=area.min_row, end_row=area.max_row, start_column=left, end_column=right)
    sheet.column_dimensions.clear()
    for key, dimension in dimensions:
        if dimension.min and dimension.min >= at:
            dimension.min += count
            dimension.max += count
            dimension.index = get_column_letter(dimension.min)
            key = dimension.index
        elif dimension.max and dimension.max >= at:
            dimension.max += count
        sheet.column_dimensions[key] = dimension


def _add_review_area(sheet, layout):
    width = layout.end - layout.start + 1
    target = layout.end + 1
    _insert_columns(sheet, target, width)
    offset = target - layout.start
    merges = list(sheet.merged_cells.ranges)
    for col in range(layout.start, layout.end + 1):
        dimensions = [dim for dim in sheet.column_dimensions.values()
                      if dim.min and dim.min <= col <= dim.max]
        dimension = copy(dimensions[-1]) if dimensions else None
        if dimension:
            dimension.index = get_column_letter(col + offset)
            dimension.min = dimension.max = col + offset
            sheet.column_dimensions[dimension.index] = dimension
        for row in range(layout.top, sheet.max_row + 1):
            source, cell = sheet.cell(row, col), sheet.cell(row, col + offset)
            cell._style = copy(source._style)
            cell.protection = copy(source.protection)
    for area in merges:
        if area.min_col < layout.start or area.max_col > layout.end or area.min_row < layout.top:
            continue
        # 数据行逐项输出；仅复制表头和横向分组，避免纵向合并隐藏各行独立结果。
        if area.max_row > layout.bottom and area.min_row != area.max_row:
            continue
        sheet.merge_cells(start_row=area.min_row, end_row=area.max_row,
                          start_column=area.min_col + offset, end_column=area.max_col + offset)
        if area.min_row > layout.bottom:
            sheet.cell(area.min_row, area.min_col + offset, safe_cell(sheet.cell(area.min_row, area.min_col).value))
    if layout.top != layout.bottom:
        sheet.cell(layout.top, target, "AI审查")
    for index in range(len(layout.groups)):
        for col, name in enumerate(("门限", "值", "结果", "备注"), target + index * 4):
            sheet.cell(layout.bottom, col, name if layout.top != layout.bottom else "AI" + name)
    if sheet.print_area:
        from openpyxl.worksheet.cell_range import CellRange
        areas = []
        for value in sheet._print_area.ranges:
            area = CellRange(str(value))
            if area.max_row >= layout.top:
                area.max_col = max(area.max_col + (width if area.max_col >= target else 0), target + width - 1)
            areas.append(str(area))
        sheet.print_area = areas
    return target


def export_excel(original, run, results, measurements=None, notes=None, sources=None):
    from .result_filters import stored_result_label
    measurements, notes, sources = measurements or {}, notes or {}, sources or {}
    workbook = load_workbook(BytesIO(original), keep_links=False)
    try:
        outputs = {}
        for sheet in workbook:
            if sheet.title.strip() in {"封面", "目录"} or not any(c.value is not None for row in sheet for c in row):
                continue
            layout = detail_header(sheet)
            outputs[sheet.title] = (layout, _add_review_area(sheet, layout))
        for result in results:
            layout, target = outputs[result.domain]
            group = sources.get(result.item_id, 0)
            if not 0 <= group < len(layout.groups):
                raise ValueError(f"{result.domain} 第 {result.row_number} 行: 审查分组与原始报告不一致")
            measured = measurements.get(result.id)
            values = [measured.ai_threshold if measured and result.status != "pending" else "",
                      measured.ai_value if measured and result.status != "pending" else "",
                      stored_result_label(result.status, result.explanation) if result.status != "pending" else "",
                      notes.get(result.item_id, "")]
            for col, value in enumerate(values, target + group * 4):
                cell = workbook[result.domain].cell(result.row_number, col, safe_cell(value))
                if col == target + group * 4 + 2:
                    # 结果文本常比原来的备注长，保留列宽、行高并让 Excel 缩小显示以免截断。
                    alignment = copy(cell.alignment)
                    alignment.shrinkToFit = True
                    cell.alignment = alignment
        out = BytesIO()
        workbook.save(out)
        return out.getvalue()
    finally:
        workbook.close()
