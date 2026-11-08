"""从测试详情补齐指标名称，将旧关键词表展开到新的 SQLite 文件。"""

import argparse
from collections import defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException


FIELDS = ("测试对象", "用例编号", "用例名称", "测试小项", "指标名称")


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def read_metrics(path):
    metrics = defaultdict(set)
    with closing(load_workbook(path, data_only=True, keep_links=False)) as workbook:
        for sheet in workbook.worksheets:
            if sheet.title.strip() in {"封面", "目录"}:
                continue
            section = None
            for row in sheet.iter_rows(max_row=min(sheet.max_row, 60)):
                labels = ["".join(str(cell.value or "").split()) for cell in row]
                if "测试详情" in labels:
                    section = row[0].row
                    continue
                if all(labels.count(field) == 1 for field in FIELDS):
                    columns = [labels.index(field) + 1 for field in FIELDS]
                    header = row[0].row
                    break
            else:
                if section is not None:
                    raise ValueError(f"{sheet.title} 的测试详情缺少完整的五字段表头")
                continue

            first_data_row = header + 1
            merged_values = {}
            for area in sheet.merged_cells.ranges:
                if area.min_col != area.max_col or area.min_col not in columns:
                    continue
                if area.min_row == header:
                    first_data_row = max(first_data_row, area.max_row + 1)
                elif area.min_row > header:
                    value = sheet.cell(area.min_row, area.min_col).value
                    for row_number in range(area.min_row, area.max_row + 1):
                        merged_values[row_number, area.min_col] = value
            for row_number in range(first_data_row, sheet.max_row + 1):
                values = [
                    merged_values.get((row_number, col), sheet.cell(row_number, col).value)
                    for col in columns
                ]
                if values[-1] is None or not str(values[-1]).strip():
                    continue
                *key, metric = ("" if value is None else str(value) for value in values)
                metrics[tuple(key)].add(metric)
    if not metrics:
        raise ValueError("Excel 中未找到带指标名称的测试详情记录")
    return metrics


def read_keywords(path, requested_table, excel_key_order):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        candidates = []
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            if requested_table is not None and table != requested_table:
                continue
            columns = db.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            names = {column[1].upper(): column[1] for column in columns}
            primary_key = {column[1].upper() for column in columns if column[5]}
            if primary_key == set("ABCD") and set("ABCDI") <= names.keys():
                candidates.append((table, names))
        if len(candidates) != 1:
            raise ValueError("需要唯一一个含 A/B/C/D 联合主键及 I 列的表，可用 --table 指定表名")
        table, names = candidates[0]
        fields = ", ".join(quote(names[name]) for name in excel_key_order + "I")
        rows = db.execute(f"SELECT {fields} FROM {quote(table)}").fetchall()
    if any(not all(isinstance(value, str) for value in row) for row in rows):
        raise ValueError("旧表存在 NULL 或非文本的键或关键词，请先检查源数据")
    return table, rows


def write_output(path, table, rows):
    # 独占创建，避免覆盖用户已有文件。
    path.touch(exist_ok=False)
    try:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f"""
                CREATE TABLE {quote(table)} (
                    A TEXT NOT NULL, B TEXT NOT NULL, C TEXT NOT NULL,
                    D TEXT NOT NULL, E TEXT NOT NULL, I TEXT NOT NULL,
                    PRIMARY KEY (A, B, C, D, E)
                )
            """)
            db.executemany(f"INSERT INTO {quote(table)} VALUES (?, ?, ?, ?, ?, ?)", rows)
    except Exception:
        path.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="旧关键词 SQLite 文件")
    parser.add_argument("--excel", type=Path, required=True, help="包含测试详情的 .xlsx")
    parser.add_argument("--output", type=Path, help="新 SQLite 文件；省略时仅预览")
    parser.add_argument("--table", help="旧库中的关键词表名")
    parser.add_argument("--excel-key-order", choices=("ABCD", "ACBD"), default="ABCD",
                        help="旧库列按 Excel 四字段顺序排列；旧库 B/C 业务含义对调时用 ACBD")
    parser.add_argument("--allow-unmatched", action="store_true",
                        help="允许仅导出匹配到指标的记录")
    args = parser.parse_args()
    try:
        table, old_rows = read_keywords(args.source.resolve(strict=True), args.table, args.excel_key_order)
        metrics = read_metrics(args.excel)
        rows = []
        unmatched = []
        for a, b, c, d, keyword in old_rows:
            key = (a, b, c, d)
            if key not in metrics:
                unmatched.append(key)
                continue
            rows.extend((*key, metric, keyword) for metric in sorted(metrics[key]))
        matched = len(old_rows) - len(unmatched)
        print(f"表 {table} | 旧库 {len(old_rows)} 条 | 匹配 {matched} 条 | "
              f"未匹配 {len(unmatched)} 条 | 展开后 {len(rows)} 条")
        for key in unmatched[:10]:
            print("未匹配四字段：", repr(key))
        if args.output is None:
            return
        if not rows:
            raise ValueError("没有匹配记录，未生成新库")
        if unmatched and not args.allow_unmatched:
            raise ValueError("有旧记录缺少匹配指标，未生成新库。仅导出匹配项时请显式加 --allow-unmatched")
        write_output(args.output, table, rows)
        print(f"已生成 {args.output}")
    except (OSError, ValueError, sqlite3.Error, BadZipFile, InvalidFileException) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
