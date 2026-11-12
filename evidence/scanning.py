import io
import json
from pathlib import Path
import subprocess
import tempfile
import warnings
import zipfile
from xml.etree import ElementTree

from django.conf import settings
from PIL import Image
from pypdf import PdfReader

TYPES = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def validate_structure(data, filename):
    suffix = Path(filename).suffix.lower()
    if suffix not in TYPES:
        raise ValueError("Supported files: PDF, DOCX, TXT, CSV, JSON, PNG, and JPEG.")
    if not data or len(data) > settings.EVIDENCE_MAX_BYTES:
        raise ValueError("Upload a nonempty file up to 10 MB.")
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data:
        raise ValueError("Antivirus test content was detected.")
    if suffix in (".txt", ".csv", ".json"):
        text = data.decode("utf-8-sig")
        if "\x00" in text or any(ord(c) < 32 and c not in "\t\r\n" for c in text):
            raise ValueError("Text files must contain ordinary UTF-8 text.")
        if text.lstrip().lower().startswith(("<!doctype html", "<html", "<script", "<?xml", "<svg")):
            raise ValueError("Active markup cannot be uploaded as text evidence.")
        if suffix == ".json":
            json.loads(text)
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("The file does not match its PDF extension.")
        pdf = PdfReader(io.BytesIO(data), strict=True)
        if pdf.is_encrypted:
            raise ValueError("Encrypted PDFs cannot be checked. Upload an unencrypted copy.")
        root = pdf.trailer["/Root"]
        if any(key in root for key in ("/OpenAction", "/AA", "/AcroForm")):
            raise ValueError("PDF actions or interactive forms are not supported.")
        names = root.get("/Names", {})
        if hasattr(names, "get_object"):
            names = names.get_object()
        if any(key in names for key in ("/JavaScript", "/EmbeddedFiles")):
            raise ValueError("PDF scripts and attachments are not supported.")
        if not len(pdf.pages) or len(pdf.pages) > 500:
            raise ValueError("PDFs must contain 1–500 pages.")
        for page in pdf.pages:
            if page.get("/AA") or page.get("/Annots"):
                raise ValueError("PDF annotations are not supported; upload a flattened copy.")
    elif suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 1500 or sum(item.file_size for item in infos) > 40 * 1024 * 1024:
                raise ValueError("The DOCX expands beyond the allowed size.")
            names = archive.namelist()
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise ValueError("This is not a valid DOCX document.")
            for member, expected in [("word/document.xml", "document"), ("[Content_Types].xml", "Types")]:
                xml = archive.read(member)
                if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                    raise ValueError("Unsafe XML was detected.")
                if ElementTree.fromstring(xml).tag.split("}")[-1] != expected:
                    raise ValueError("Malformed DOCX document structure.")
            for item in infos:
                name = item.filename.lower()
                if name.startswith("/") or ".." in Path(name).parts or "vbaproject" in name or "/embeddings/" in name or item.flag_bits & 1:
                    raise ValueError("DOCX macros, embedded objects, or unsafe entries are not supported.")
                if name.endswith(".rels"):
                    xml = archive.read(item)
                    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                        raise ValueError("Unsafe XML was detected.")
                    for relation in ElementTree.fromstring(xml):
                        if relation.get("TargetMode") == "External" and not relation.get("Type", "").endswith("/hyperlink"):
                            raise ValueError("Externally linked document content is not supported.")
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != ("PNG" if suffix == ".png" else "JPEG"):
                    raise ValueError("The image does not match its extension.")
                image.verify()
    return TYPES[suffix]


def scan_file(data, filename):
    try:
        validate_structure(data, filename)
    except Exception:
        return "rejected", "structural-v1", "The file failed type, size, or content checks. Upload a supported, flattened document."
    mode = settings.EVIDENCE_SCAN_MODE
    if mode == "basic" and settings.DEBUG:
        return "clean", "development-structural-v1", "Development structural checks passed. Malware scanning was not performed."
    if mode != "external":
        return "quarantined", "unconfigured", "A malware scanner must be configured before this file can be released."
    try:
        command = json.loads(settings.EVIDENCE_SCAN_COMMAND)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError
        with tempfile.TemporaryDirectory(prefix="cyber-evidence-scan-") as directory:
            path = Path(directory) / ("evidence" + Path(filename).suffix.lower())
            path.write_bytes(data)
            # No shell and no document-controlled arguments. Scanner output is not logged.
            result = subprocess.run([*command, str(path)], timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return "clean", "external-scanner", "Structural and configured malware checks passed."
        if result.returncode == 1:
            return "rejected", "external-scanner", "The configured malware scanner rejected this file."
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    return "quarantined", "external-scanner", "The scanner is unavailable or failed. The file remains quarantined; a reviewer can retry."
