"""清理旧版冗余日志；默认只统计，不修改数据。"""
import argparse
from datetime import datetime
from pathlib import Path
import sqlite3

from sqlalchemy import delete, func, select
from .db import Session, engine
from .models import (Audit, ChangeRequest, Item, ItemNote, ItemSource, ItemReviewState, Report,
                     Result, ResultMeasurements, ResultWork, Run, RunControl)
from .services import RECORDED_ACTIONS


def clean_audit(db, apply=False):
    noise = ~Audit.action.in_(RECORDED_ACTIONS)
    count = db.scalar(select(func.count()).select_from(Audit).where(noise))
    snapshots = 0
    for row in db.scalars(select(Audit).where(Audit.action == "run.start")).yield_per(100):
        if "keywords" in (row.after or {}):
            snapshots += 1
            if apply:
                row.after = {key: value for key, value in row.after.items() if key != "keywords"}
    if apply:
        db.execute(delete(Audit).where(noise))
    return {"redundant_logs": count, "duplicate_snapshots": snapshots}


def reset_history(db, apply=False):
    counts = {"reports": db.scalar(select(func.count()).select_from(Report)),
              "runs": db.scalar(select(func.count()).select_from(Run)),
              "results": db.scalar(select(func.count()).select_from(Result))}
    if apply:
        from sqlalchemy import update
        db.execute(update(ChangeRequest).values(result_id=None))
        for model in (ItemReviewState, ResultMeasurements, ResultWork, Result, RunControl,
                      Run, ItemNote, ItemSource, Item, Report):
            db.execute(delete(model))
        db.execute(delete(Audit).where(Audit.action.in_(
            {"report.upload", "report.download", "report.clear", "run.start", "run.finished", "run.export", "run.clear", "item.review_state"})))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="备份后实际清理")
    parser.add_argument("--vacuum", action="store_true", help="清理后压缩 SQLite；须先停止 API 和 Worker")
    parser.add_argument("--reset-history", action="store_true",
                        help="清空所有用户的报告及审查历史和原始文件，保留用户、关键词、审批；须先停止服务")
    args = parser.parse_args()
    if args.vacuum and not args.apply:
        parser.error("--vacuum 必须与 --apply 一起使用")
    if args.apply:
        if engine.dialect.name != "sqlite":
            parser.error("此脚本的备份和压缩仅适用于 SQLite")
        source = Path(engine.url.database).resolve()
        backup = source.with_name(source.name + ".backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
        with engine.connect() as connection, sqlite3.connect(backup) as target:
            connection.connection.driver_connection.backup(target)
        print("备份：", backup)
    report_ids = []
    with Session() as db:
        print(clean_audit(db, apply=args.apply))
        if args.reset_history:
            if args.apply:
                report_ids = list(db.scalars(select(Report.id)))
            print("历史重置：", reset_history(db, apply=args.apply))
        if args.apply:
            db.commit()
    if report_ids:
        from uuid import UUID
        from .config import settings
        for report_id in report_ids:
            try:
                # 仅删除系统 UUID 对应的原始上传文件。
                if str(UUID(report_id)) != report_id:
                    raise ValueError("invalid report ID")
                (settings.data_dir / (report_id + ".xlsx")).unlink(missing_ok=True)
            except (OSError, ValueError):
                print("原始文件未删除，请管理员检查：", report_id)
    if args.vacuum:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql("VACUUM")
        print("已压缩数据库；备份仍保留。")
    if not args.apply:
        print("仅统计，未修改数据库。")


if __name__ == "__main__":
    main()
