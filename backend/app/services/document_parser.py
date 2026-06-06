from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    file_path: Path
    file_type: str
    page_count: int | None
    text: str
    pages: list[ParsedPage]


def parse_document(file_path: Path) -> ParsedDocument:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(file_path)

    raise ValueError(f"unsupported file type: {suffix}")


def parse_pdf(file_path: Path) -> ParsedDocument:
    reader = PdfReader(str(file_path))
    pages: list[ParsedPage] = []

    for index, page in enumerate(reader.pages, start=1):
        pages.append(ParsedPage(page_number=index, text=page.extract_text() or ""))

    text = "\n\n".join(page.text.strip() for page in pages if page.text.strip())

    return ParsedDocument(
        file_path=file_path,
        file_type="pdf",
        page_count=len(reader.pages),
        text=text,
        pages=pages,
    )
