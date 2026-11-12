import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from django.conf import settings
from .errors import ProcessingError


def prepare_document(data, filename):
    root = settings.BASE_DIR / ".local" / "ai-temp"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    if not data or len(data) > settings.EVIDENCE_MAX_BYTES:
        raise ProcessingError("file_limit", "The document is outside the processing size limit. Continue manual review.")
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        path = Path(temporary) / ("source" + Path(filename).suffix.lower())
        output = Path(temporary) / "prepared.json"
        path.write_bytes(data)
        config = {"max_pages": settings.AI_MAX_PAGES, "max_chars": settings.AI_MAX_TEXT_CHARS, "tesseract": settings.AI_TESSERACT_COMMAND}
        # The parser/OCR process receives no database, cloud-storage, or AI credentials.
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")}
        proc = subprocess.Popen([sys.executable, "-m", "ai_review.prepare_file", str(path), str(output), json.dumps(config)],
            cwd=settings.BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
        try:
            proc.wait(timeout=settings.AI_PREPARATION_TIMEOUT)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait()
            raise ProcessingError("preparation_timeout", "Document preparation timed out. Retry or continue manual review.") from None
        if proc.returncode or not output.is_file() or output.stat().st_size > 2_000_000:
            raise ProcessingError("preparation_failed", "Document preparation failed. Continue manual review or submit a simpler copy.")
        result = json.loads(output.read_text())
        if result.get("error"):
            code = result["error"]
            message = "OCR is unavailable. Install Tesseract or continue manual review." if code == "ocr_unavailable" else "Document preparation could not safely read the complete file. Check the format/size or continue manual review."
            raise ProcessingError(code, message)
        return result
