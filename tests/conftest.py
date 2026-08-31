import os

# Must be set before any test module imports PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
