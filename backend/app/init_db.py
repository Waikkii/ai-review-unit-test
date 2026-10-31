from .config import settings
from .db import Base, engine
from . import models

def main():
    settings.validate_runtime()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    print("Database initialized (schema v1).")

if __name__ == "__main__":
    main()
