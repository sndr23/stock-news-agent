# filepath: tests/conftest.py
"""pytest 配置：把项目根目录加入 sys.path，便于 import src.*"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
