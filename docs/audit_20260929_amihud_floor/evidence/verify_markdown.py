from pathlib import Path
import sys


needle = "?" + "?"
for name in sys.argv[1:]:
    path = Path(name)
    text = path.read_text(encoding="utf-8")
    assert needle not in text, f"{path} content integrity failure"
    print(path, "OK", len(text))
