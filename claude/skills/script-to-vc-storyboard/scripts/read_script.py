"""Dump a script (.docx, .txt, .md) to UTF-8 text for Claude to read: paragraphs and tables in document order.

    python read_script.py <script.docx> [--out <file>]

Default output: <script folder>/VC分鏡/腳本內容.txt. Table rows print as "R<n>: cell | cell", a cell's own
line breaks as " / ".
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph(p: ET.Element) -> str:
    out = []
    for run in p.iter(W + "r"):
        for el in run:
            if el.tag == W + "t":
                out.append(el.text or "")
            elif el.tag in (W + "br", W + "cr"):
                out.append("\n")
            elif el.tag == W + "tab":
                out.append(" ")
    return "".join(out).strip()


def _cell(tc: ET.Element) -> str:
    lines = [ln.strip() for p in tc.findall(W + "p") for ln in _paragraph(p).split("\n")]
    return " / ".join(ln for ln in lines if ln)


def docx_lines(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z, z.open("word/document.xml") as f:
        body = ET.fromstring(f.read()).find(W + "body")
    lines: list[str] = []
    for child in body if body is not None else []:
        if child.tag == W + "p":
            text = _paragraph(child)
            if text:
                lines.append(text)
        elif child.tag == W + "tbl":
            lines.append("[TABLE]")
            for n, tr in enumerate(child.findall(W + "tr")):
                lines.append(f"R{n}: " + " | ".join(_cell(tc) for tc in tr.findall(W + "tc")))
            lines.append("[/TABLE]")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="把腳本轉成純文字給 Claude 讀")
    ap.add_argument("script", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    src: Path = args.script
    if not src.is_file():
        print(f"找不到腳本：{src}", file=sys.stderr)
        return 1
    if src.suffix.lower() == ".docx":
        try:
            lines = docx_lines(src)
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            print("無法讀取這個 Word 檔，請確認是 .docx（舊版 .doc 請先另存為 .docx）", file=sys.stderr)
            return 1
    else:
        lines = src.read_text(encoding="utf-8-sig").splitlines()
    out: Path = args.out or src.parent / "VC分鏡" / "腳本內容.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已寫出 {out}（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
