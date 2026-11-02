"""Additive upgrade for existing installations; never rewrites report or result data."""
from .config import settings
from .db import engine
from .models import ItemNote, ResultMeasurements


def main():
    settings.validate_runtime()
    for table in (ItemNote.__table__, ResultMeasurements.__table__):
        table.create(engine, checkfirst=True)
    print("Schema upgrade complete: item notes and AI measurements.")


if __name__ == "__main__":
    main()
