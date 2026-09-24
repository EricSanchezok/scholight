"""Exercise the real native PDF stack in each final, unmodified Extract image."""

from __future__ import annotations

import pymupdf

from scholight.web_extract.engine import _pdf
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.heap import _load_trim, release_unused_heap


def main() -> None:
    assert _load_trim() is not None, "Final Linux image must expose the native heap release hook"
    assert isinstance(release_unused_heap(), bool)
    with pymupdf.open() as document:
        document.set_metadata({"title": "Extract PDF smoke", "author": "Scholight"})
        document.new_page().insert_text((72, 72), "Scholight real PDF extraction works.")
        data = document.tobytes()
    for _ in range(3):
        result = _pdf(data)
        assert "Scholight real PDF extraction works." in result.content
        assert result.title == "Extract PDF smoke"
        assert result.author == "Scholight"
    try:
        _pdf(b"%PDF-invalid")
    except ExtractError as error:
        assert (error.code, error.status_code) == ("extraction_failed", 422)
    else:
        raise AssertionError("Malformed PDF was accepted")


if __name__ == "__main__":
    main()
