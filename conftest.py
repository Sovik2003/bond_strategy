"""Корень проекта: добавляем src и корень в путь импорта.

Корень нужен, чтобы работали импорты вида `from tests.facts import ...`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
