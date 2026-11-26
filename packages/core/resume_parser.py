import io

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_PAGES = 20
MAX_TEXT_CHARS = 40_000
PDF_MAGIC = b"%PDF-"


class ResumeParseError(Exception):
    pass


def extract_text(data: bytes) -> str:
    if not data.startswith(PDF_MAGIC):
        raise ResumeParseError("this file is not a PDF")

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ResumeParseError("this PDF is password protected")
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PAGES])
    except PyPdfError as exc:
        raise ResumeParseError(f"this PDF could not be read: {exc}") from exc

    if not text.strip():
        raise ResumeParseError("no selectable text found in this PDF")

    return text[:MAX_TEXT_CHARS]
