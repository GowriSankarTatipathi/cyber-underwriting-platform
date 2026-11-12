"""Bounded document preparation in a separate process; never connects to the network."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile
from xml.etree import ElementTree


def prepare(path, max_pages, max_chars, tesseract):
    from PIL import Image
    passages, warnings = [], []
    total = 0

    def add(text, locator, method, confidence=None):
        nonlocal total
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
        total += len(text)
        if total > max_chars:
            raise ValueError("text_limit")
        for block, chunk in enumerate(textwrap.wrap(text, width=1500, replace_whitespace=False, drop_whitespace=True), 1):
            passages.append({"id": f"p{len(passages) + 1:04d}", "locator": f"{locator}, block {block}",
                "method": method, "text": chunk, "sha256": hashlib.sha256(chunk.encode()).hexdigest(), "ocr_confidence": confidence})

    def ocr(image, locator):
        if image.width * image.height > 20_000_000:
            raise ValueError("image_limit")
        image = image.convert("RGB")
        image.thumbnail((2400, 3200))
        target = path.parent / "ocr-page.png"
        output = path.parent / "ocr-output"
        image.save(target)
        try:
            subprocess.run([tesseract, str(target), str(output), "-l", "eng", "--psm", "3", "tsv"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        except FileNotFoundError:
            raise ValueError("ocr_unavailable") from None
        except (subprocess.SubprocessError, OSError):
            raise ValueError("ocr_failed") from None
        tsv = output.with_suffix(".tsv")
        if not tsv.is_file() or tsv.stat().st_size > 2_000_000:
            raise ValueError("ocr_limit")
        lines, scores = {}, []
        with tsv.open() as stream:
            for row in csv.DictReader(stream, delimiter="\t"):
                word = row.get("text", "").strip()
                if word:
                    key = tuple(row.get(name) for name in ("block_num", "par_num", "line_num"))
                    lines.setdefault(key, []).append(word)
                    score = float(row.get("conf", -1))
                    if score >= 0:
                        scores.append(score)
        confidence = round(sum(scores) / len(scores), 1) if scores else 0
        if confidence < 80:
            warnings.append(f"{locator}: OCR quality is low; compare citations with the original image.")
        for index, words in enumerate(lines.values(), 1):
            add(" ".join(words), f"{locator}, line {index}", "ocr", confidence)

    suffix = path.suffix.lower()
    if suffix in (".txt", ".csv", ".json"):
        for line, text in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            add(text, f"Line {line}", "text")
    elif suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            if sum(row.file_size for row in archive.infolist()) > 40 * 1024 * 1024:
                raise ValueError("document_limit")
            parts = [name for name in archive.namelist() if name == "word/document.xml" or name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes")) and name.endswith(".xml")]
            for part in sorted(parts):
                xml = archive.read(part)
                if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                    raise ValueError("unsafe_xml")
                root = ElementTree.fromstring(xml)
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                for index, paragraph in enumerate(root.iter(ns + "p"), 1):
                    add("".join(node.text or "" for node in paragraph.iter(ns + "t")), f"{Path(part).stem}, paragraph {index}", "docx")
            images = [name for name in archive.namelist() if name.startswith("word/media/")]
            if len(images) > max_pages:
                raise ValueError("page_limit")
            for index, name in enumerate(images, 1):
                try:
                    with Image.open(io.BytesIO(archive.read(name))) as image:
                        ocr(image, f"Embedded image {index}")
                except (ValueError, OSError):
                    raise ValueError("embedded_image_unreadable") from None
    elif suffix == ".pdf":
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(path) as document:
            if not 1 <= len(document) <= max_pages:
                raise ValueError("page_limit")
            for index in range(len(document)):
                page = document[index]
                textpage = page.get_textpage()
                if textpage.count_chars() > max_chars:
                    raise ValueError("text_limit")
                text = textpage.get_text_range()
                textpage.close()
                for line, value in enumerate(text.splitlines(), 1):
                    add(value, f"Page {index + 1}, line {line}", "pdf-text")
                # OCR image-bearing pages too: a text header must not hide a scanned log.
                has_image = any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
                if not text.strip() or has_image:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError("invalid_page")
                    bitmap = page.render(scale=min(2.5, 2400 / width, 3200 / height))
                    ocr(bitmap.to_pil(), f"Page {index + 1}")
                    bitmap.close()
                page.close()
    elif suffix in (".png", ".jpg", ".jpeg"):
        with Image.open(path) as image:
            ocr(image, "Image 1")
    else:
        raise ValueError("unsupported_format")
    if not passages:
        raise ValueError("no_text")
    return {"passages": passages, "warnings": warnings[:30]}


def main():
    path, output = Path(sys.argv[1]), Path(sys.argv[2])
    config = json.loads(sys.argv[3])
    try:
        if os.name == "posix":
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (80, 85))
            resource.setrlimit(resource.RLIMIT_FSIZE, (12 * 1024 * 1024, 12 * 1024 * 1024))
            if sys.platform.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
        result = prepare(path, **config)
    except Exception as error:
        known = {"text_limit", "image_limit", "page_limit", "document_limit", "ocr_unavailable", "ocr_failed", "ocr_limit", "no_text", "unsupported_format", "unsafe_xml", "embedded_image_unreadable", "invalid_page"}
        code = str(error) if isinstance(error, ValueError) and str(error) in known else "preparation_failed"
        result = {"error": code}
    output.write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
