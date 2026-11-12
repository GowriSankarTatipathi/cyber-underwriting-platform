import hashlib
import io
import json
import shutil
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
import zipfile

from django.test import SimpleTestCase, override_settings
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from .errors import ProcessingError
from .preparation import prepare_document
from .providers import demo_extract, extract
from .schema import Extraction
from .validation import validate_extraction, validate_fact


LOG = b"Service: Ordering\nScope: Ordering and required dependencies.\nTotal recovery time: 7 hours (420 minutes).\nRecovered data age: 35 minutes.\nFunctional checks: passed; created and retrieved a test order.\n"


def passage(text, key="p0001"):
    return {"id": key, "text": text, "locator": "Page 1, line 1", "method": "text",
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "ocr_confidence": None}


def image_bytes(format="PNG"):
    image = Image.new("RGB", (1800, 620), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for i, line in enumerate(LOG.decode().splitlines()):
        draw.text((50, 40 + i * 95), line, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format=format)
    return output.getvalue()


class CitationTests(SimpleTestCase):
    def check(self, field, value, text, quote=None, key="p0001"):
        return validate_fact({"field": field, "value": value, "citations": [{"passage_id": key, "quote": quote or text}]}, [passage(text)])

    def test_seven_hours_has_exact_offsets_and_hash(self):
        text = "Log: Total recovery time: seven hours. End."
        quote = "Total recovery time: seven hours."
        fact = self.check("restore_duration_minutes", "420", text, quote)
        self.assertTrue(fact["valid"])
        citation = fact["citations"][0]
        self.assertEqual(text[citation["start"]:citation["end"]], quote)
        self.assertEqual(citation["passage_sha256"], passage(text)["sha256"])

    def test_mismatched_missing_and_invented_citations_fail(self):
        for value, quote, key in [("180", None, "p0001"), ("420", "Restore duration: seven hours", "p0001"), ("420", None, "another-file")]:
            with self.subTest(value=value, quote=quote, key=key):
                self.assertFalse(self.check("restore_duration_minutes", value, "Total recovery time: 7 hours", quote, key)["valid"])

    def test_targets_ambiguous_times_and_wrong_measurement_are_not_results(self):
        for text in ("Restore target: 4 hours", "Restore duration: 4 hours; required maximum 4 hours",
            "Restore duration: 4 hours, later 3 hours", "Recovered data age: 4 hours", "Backup completed: 4 hours"):
            with self.subTest(text=text):
                self.assertFalse(self.check("restore_duration_minutes", "240", text)["valid"])

    def test_nonfinite_negative_and_oversized_values_fail(self):
        for value in ("NaN", "Infinity", "-1", "10000001", "abc"):
            with self.subTest(value=value):
                self.assertFalse(self.check("restore_duration_minutes", value, "Restore duration: 7 hours")["valid"])

    def test_failed_checks_and_backup_do_not_turn_into_passes(self):
        for text in ("Functional checks: failed", "Functional checks: not passed", "Functional checks: not successful", "Functional checks did not work"):
            self.assertFalse(self.check("functional_checks_passed", "true", text)["valid"])
            self.assertTrue(self.check("functional_checks_passed", "false", text)["valid"])
        for text in ("Backup completed with failed jobs", "Backup not successful", "Backup partially completed"):
            self.assertFalse(self.check("backup_status", "success", text)["valid"])

    def test_schema_rejects_extra_fields_and_missing_sources(self):
        for payload in ({"facts": [], "warnings": [], "decision": "approve"},
            {"facts": [{"field": "restore_duration_minutes", "value": "420", "citations": []}], "warnings": []}):
            with self.assertRaises(ValidationError):
                validate_extraction(payload, [])


class PreparationTests(SimpleTestCase):
    def test_text_and_docx_preserve_locators_and_cited_values(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Total recovery time: 7 hours</w:t></w:r></w:p></w:body></w:document>')
        for name, data, method in (("log.txt", LOG, "text"), ("log.docx", archive.getvalue(), "docx")):
            with self.subTest(name=name):
                prepared = prepare_document(data, name)
                payload, _, _ = demo_extract(prepared["passages"])
                facts, _ = validate_extraction(payload, prepared["passages"])
                fact = next(row for row in facts if row["field"] == "restore_duration_minutes")
                self.assertTrue(fact["valid"])
                self.assertEqual(fact["normalized_value"], "420")
                self.assertEqual(fact["citations"][0]["method"], method)

    def test_native_pdf_text_is_read_without_ocr(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 18 Tf 40 700 Td (Total recovery time: 7 hours) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        output = io.BytesIO()
        writer.write(output)
        with override_settings(AI_TESSERACT_COMMAND="/missing/tesseract"):
            prepared = prepare_document(output.getvalue(), "native.pdf")
        self.assertIn("7 hours", prepared["passages"][0]["text"])
        self.assertEqual(prepared["passages"][0]["method"], "pdf-text")

    @skipUnless(shutil.which("tesseract"), "Install Tesseract for real OCR integration coverage")
    def test_image_and_scanned_pdf_ocr_find_cited_seven_hour_result(self):
        for name, format in (("scan.png", "PNG"), ("scan.pdf", "PDF")):
            with self.subTest(name=name):
                prepared = prepare_document(image_bytes(format), name)
                payload, _, _ = demo_extract(prepared["passages"])
                facts, _ = validate_extraction(payload, prepared["passages"])
                fact = next(row for row in facts if row["field"] == "restore_duration_minutes")
                self.assertTrue(fact["valid"])
                self.assertEqual(fact["normalized_value"], "420")
                self.assertEqual(fact["citations"][0]["method"], "ocr")

    @override_settings(AI_TESSERACT_COMMAND="/missing/tesseract")
    def test_missing_ocr_returns_actionable_failure(self):
        with self.assertRaises(ProcessingError) as error:
            prepare_document(image_bytes(), "scan.png")
        self.assertEqual(error.exception.code, "ocr_unavailable")

    @override_settings(AI_MAX_TEXT_CHARS=10)
    def test_large_text_fails_instead_of_silently_truncating(self):
        with self.assertRaises(ProcessingError) as error:
            prepare_document(LOG, "log.txt")
        self.assertEqual(error.exception.code, "text_limit")

    @override_settings(AI_MAX_PAGES=1)
    def test_page_limit_and_malformed_files_fail_cleanly(self):
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        writer.add_blank_page(width=600, height=800)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaises(ProcessingError) as error:
            prepare_document(output.getvalue(), "long.pdf")
        self.assertEqual(error.exception.code, "page_limit")
        with self.assertRaises(ProcessingError):
            prepare_document(b"not a pdf", "invalid.pdf")

    def test_xml_entities_are_rejected(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("word/document.xml", '<!DOCTYPE root [<!ENTITY x SYSTEM "file:///private-data">]><root>&x;</root>')
        with self.assertRaises(ProcessingError) as error:
            prepare_document(archive.getvalue(), "log.docx")
        self.assertEqual(error.exception.code, "unsafe_xml")


@override_settings(AI_ALLOW_EXTERNAL=True, OPENAI_API_KEY="test-key-never-sent", AI_PROVIDER_TIMEOUT=60)
class ProviderTests(SimpleTestCase):
    def test_structured_request_is_stateless_and_contains_only_passages(self):
        passages = [passage("Total recovery time: 7 hours")]
        payload, _, _ = demo_extract(passages)
        parsed = Extraction.model_validate(payload)
        job = SimpleNamespace(provider="openai", model_name="configured-model")
        with patch("openai.OpenAI") as sdk:
            client = sdk.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status="completed", output_parsed=parsed, id="response-id", model="actual-model")
            result, response_id, model = extract(job, passages)
        args = client.responses.parse.call_args.kwargs
        self.assertIs(args["store"], False)
        self.assertNotIn("tools", args)
        self.assertEqual(args["model"], "configured-model")
        sent = json.loads(args["input"][1]["content"])["passages"][0]
        self.assertEqual(set(sent), {"id", "locator", "text"})
        self.assertEqual((result, response_id, model), (payload, "response-id", "actual-model"))
        self.assertEqual(sdk.call_args.kwargs["base_url"], "https://api.openai.com/v1")

    def test_refusal_and_incomplete_output_remain_manual_review_failures(self):
        job = SimpleNamespace(provider="openai", model_name="configured-model")
        for status in ("incomplete", "completed"):
            with patch("openai.OpenAI") as sdk:
                sdk.return_value.__enter__.return_value.responses.parse.return_value = SimpleNamespace(status=status, output_parsed=None)
                with self.assertRaises(ProcessingError) as error:
                    extract(job, [])
                self.assertEqual(error.exception.code, "incomplete_response")

    def test_missing_external_configuration_never_creates_client(self):
        with override_settings(AI_ALLOW_EXTERNAL=False), patch("openai.OpenAI") as sdk:
            with self.assertRaises(ProcessingError):
                extract(SimpleNamespace(provider="openai", model_name="configured-model"), [])
            sdk.assert_not_called()

    def test_connection_failure_is_retryable_without_leaking_provider_detail(self):
        import httpx2 as httpx
        from openai import APIConnectionError
        with patch("openai.OpenAI") as sdk:
            sdk.return_value.__enter__.return_value.responses.parse.side_effect = APIConnectionError(message="PRIVATE RESPONSE", request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
            with self.assertRaises(ProcessingError) as error:
                extract(SimpleNamespace(provider="openai", model_name="configured-model"), [])
        self.assertTrue(error.exception.retryable)
        self.assertNotIn("PRIVATE", error.exception.message)
