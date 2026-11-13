import os
from pathlib import Path
from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand, CommandError
from operations.recovery import create_bundle, verify_bundle


class Command(BaseCommand):
    help = "Create an encrypted database/evidence backup or verify it in an isolated temporary PostgreSQL database."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["key", "create", "verify"])
        parser.add_argument("--key-file", required=True)
        parser.add_argument("--bundle")

    def handle(self, action, key_file, bundle, **options):
        if action == "key":
            path = Path(key_file)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                raise CommandError("Key already exists; leaving it unchanged.") from None
            self.stdout.write("Private recovery key created. Keep a separate protected copy; its contents are never printed.")
            return
        if not bundle:
            raise CommandError("Provide --bundle with a new output path (create) or existing encrypted bundle (verify).")
        try:
            if action == "create":
                result = create_bundle(bundle, key_file)
                self.stdout.write(f"Encrypted bundle created: {result['tables']} database tables, {result['files']} evidence files.")
            else:
                result = verify_bundle(bundle, key_file)
                self.stdout.write(f"Restore verified: {result.database_tables} tables, {result.evidence_files} files in {result.elapsed_seconds:.1f}s. Temporary database removed. Drill record {result.pk}.")
        except Exception:
            raise CommandError("Recovery operation failed. Check key permissions, bundle integrity, database privileges/client version, and private evidence availability. No working database is overwritten by this command.") from None
