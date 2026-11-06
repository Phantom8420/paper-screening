"""Identify a file by its content, not just its extension."""
import re
import zipfile
from pathlib import Path

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc (also .xls/.ppt, which we treat as doc)
TEX_HINT = re.compile(rb"\\(documentclass|begin\{|section\{|usepackage|input\{|title\{)")

EXT_KIND = {".docx": "docx", ".doc": "doc", ".rtf": "rtf", ".tex": "tex", ".pdf": "pdf", ".zip": "zip"}


def sniff(path: Path) -> str:
    """Return docx | doc | rtf | tex | pdf | zip | corrupt | unknown."""
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(OLE2):
        return "doc"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                return "docx" if "word/document.xml" in z.namelist() else "zip"
        except zipfile.BadZipFile:
            return "corrupt"
    if b"\x00" not in head and TEX_HINT.search(head):
        return "tex"
    if path.suffix.lower() in (".docx", ".doc", ".zip", ".pdf"):
        return "corrupt"  # known binary extension with unrecognisable content
    return "unknown"
