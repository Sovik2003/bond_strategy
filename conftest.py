"""Корень проекта: добавляем src и корень в пути импорта.

Streamlit запускает скрипты из корня, но страницы в `pages/` исполняются с
другим sys.path — поэтому общий `ui_portfolio` должен находиться через корень
проекта, а не относительно файла страницы.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
