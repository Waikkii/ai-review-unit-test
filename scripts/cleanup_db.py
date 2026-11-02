"""从项目根目录运行；参数参见 --help。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.cleanup import main

if __name__ == "__main__":
    main()
