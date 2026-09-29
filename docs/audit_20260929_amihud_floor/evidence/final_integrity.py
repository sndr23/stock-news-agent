from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "evidence" / "markdown_integrity.txt"
needle_text = "?" + "?"
needle_bytes = b"?" * 2
lines = []
for path in sorted(p for p in ROOT.rglob("*") if p.is_file() and p != LOG):
    raw = path.read_bytes()
    if needle_bytes in raw:
        raise AssertionError(f"repeated question-mark sequence in {path}")
    if path.suffix.lower() == ".md":
        text = path.read_text(encoding="utf-8")
        assert needle_text not in text, f"{path} content integrity failure"
        lines.append(f"{path.relative_to(ROOT)} OK UTF-8 chars={len(text)}")
lines.append(f"PASS: {len(lines)} Markdown files read back; no repeated question-mark sequence in output files")
LOG.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
for path in ROOT.rglob("*"):
    if path.is_file() and needle_bytes in path.read_bytes():
        raise AssertionError(f"repeated question-mark sequence in {path}")
print("\n".join(lines))
