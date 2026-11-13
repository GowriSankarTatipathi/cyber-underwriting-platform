"""Encrypted, consistent PostgreSQL + evidence bundles and isolated restore drills."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
import psycopg
from psycopg import sql
from evidence.models import EvidenceVersion
from evidence.storage import read_private
from workspaces.models import CaseFile
from .models import RecoveryDrill

MAGIC = b"CYBER-RECOVERY-1\n"
CHUNK = 1024 * 1024
MAX_ARCHIVE = 8 * 1024 ** 3


class RecoveryError(Exception):
    pass


def key_from(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise RecoveryError("The recovery key file must be private (chmod 600).")
    return Fernet(path.read_bytes().strip())


def encrypt(source, destination, key):
    with open(source, "rb") as plain, open(destination, "xb") as sealed:
        os.chmod(destination, 0o600)
        sealed.write(MAGIC)
        index = 0
        while True:
            chunk = plain.read(CHUNK)
            token = key.encrypt(index.to_bytes(8, "big") + chunk)
            sealed.write(struct.pack(">I", len(token)))
            sealed.write(token)
            index += 1
            if not chunk:
                break


def decrypt(source, destination, key):
    try:
        with open(source, "rb") as sealed, open(destination, "xb") as plain:
            os.chmod(destination, 0o600)
            if sealed.read(len(MAGIC)) != MAGIC:
                raise RecoveryError("Unsupported recovery bundle.")
            index, total = 0, 0
            while True:
                length = sealed.read(4)
                if len(length) != 4:
                    raise RecoveryError("Incomplete recovery bundle.")
                size = struct.unpack(">I", length)[0]
                if not 1 <= size <= CHUNK * 2:
                    raise RecoveryError("Invalid encrypted frame.")
                frame = key.decrypt(sealed.read(size))
                if len(frame) < 8 or int.from_bytes(frame[:8], "big") != index:
                    raise RecoveryError("Reordered or invalid recovery bundle.")
                chunk = frame[8:]
                if not chunk:
                    if sealed.read(1):
                        raise RecoveryError("Unexpected data after bundle completion.")
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise RecoveryError("Recovery bundle exceeds the 8 GB pilot limit.")
                plain.write(chunk)
                index += 1
    except InvalidToken:
        raise RecoveryError("Recovery key or bundle integrity check failed.") from None


def digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK):
            sha.update(chunk)
    return sha.hexdigest()


def db_parameters(name=None):
    db = settings.DATABASES["default"]
    return {"dbname": name or db["NAME"], "user": db["USER"], "password": db["PASSWORD"], "host": db["HOST"], "port": db["PORT"]}


def pg_tool(binary, database, *args):
    params = db_parameters(database)
    env = {**os.environ, "PGPASSWORD": str(params["password"]), "PGHOST": str(params["host"]), "PGPORT": str(params["port"]), "PGUSER": str(params["user"]), "PGDATABASE": str(database)}
    try:
        subprocess.run([binary, *map(str, args)], env=env, check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (subprocess.SubprocessError, OSError):
        raise RecoveryError(f"{binary} failed. Check client/server versions and database permissions; no working database was overwritten.") from None


def fingerprints(conn):
    result = {}
    with conn.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(sql.SQL("SELECT row_to_json(t)::text FROM public.{} t ORDER BY row_to_json(t)::text").format(sql.Identifier(table)))
            count, sha = 0, hashlib.sha256()
            while rows := cursor.fetchmany(500):
                for row in rows:
                    sha.update(row[0].encode() + b"\n")
                    count += 1
            result[table] = {"rows": count, "sha256": sha.hexdigest()}
    return result


def temporary_root():
    root = settings.BASE_DIR / ".local" / "recovery-work"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def create_bundle(destination, key_path):
    destination = Path(destination)
    if destination.exists() or connection.in_atomic_block:
        raise RecoveryError("Use a new bundle path outside an existing database transaction.")
    key = key_from(key_path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=temporary_root()) as folder:
        folder = Path(folder)
        dump, archive_path = folder / "database.dump", folder / "bundle.tar"
        manifest = {"format": 1, "created_at": timezone.now().isoformat(), "files": []}
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SELECT pg_export_snapshot()")
                snapshot = cursor.fetchone()[0]
            pg_tool("pg_dump", settings.DATABASES["default"]["NAME"], "--format=custom", "--no-owner", "--no-acl", "--snapshot=" + snapshot, "--file=" + str(dump))
            manifest["tables"] = fingerprints(connection)
            with tarfile.open(archive_path, "w") as archive:
                archive.add(dump, arcname="database.dump", recursive=False)
                manifest["database_sha256"] = digest(dump)
                def add(name, body, reference):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(body), 0o600
                    archive.addfile(info, io.BytesIO(body))
                    manifest["files"].append({"name": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), **reference})
                for version in EvidenceVersion.objects.order_by("pk").iterator():
                    add("evidence/" + str(version.pk), read_private(version), {"type": "evidence", "id": str(version.pk)})
                for item in CaseFile.objects.order_by("pk").iterator():
                    root = Path(settings.PRIVATE_FILE_ROOT).resolve()
                    path = (root / item.storage_key).resolve()
                    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > settings.EVIDENCE_MAX_BYTES:
                        raise RecoveryError("A legacy case file could not be safely backed up.")
                    add("legacy/" + str(item.pk), path.read_bytes(), {"type": "legacy", "id": str(item.pk)})
                raw = json.dumps(manifest, sort_keys=True).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode = len(raw), 0o600
                archive.addfile(info, io.BytesIO(raw))
        if archive_path.stat().st_size > MAX_ARCHIVE:
            raise RecoveryError("Bundle exceeds the 8 GB pilot limit; use the staging backup service for larger deployments.")
        encrypted = folder / "encrypted.bundle"
        encrypt(archive_path, encrypted, key)
        # Exclusive creation also protects a destination created after the first check.
        with destination.open("xb") as output:
            destination.chmod(0o600)
            try:
                with encrypted.open("rb") as source:
                    while chunk := source.read(CHUNK):
                        output.write(chunk)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
    return {"tables": len(manifest["tables"]), "files": len(manifest["files"]), "sha256": digest(destination)}


def verify_bundle(bundle, key_path):
    started = time.monotonic()
    name = "cyber_restore_drill_" + uuid.uuid4().hex
    created = False
    key = key_from(key_path)
    with tempfile.TemporaryDirectory(dir=temporary_root()) as folder:
        folder = Path(folder)
        archive_path = folder / "decrypted.tar"
        decrypt(bundle, archive_path, key)
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            if any(not member.isfile() for member in members):
                raise RecoveryError("Recovery archive contains non-file entries.")
            info = archive.getmember("manifest.json")
            if info.size > 5_000_000:
                raise RecoveryError("Invalid recovery manifest size.")
            manifest = json.load(archive.extractfile(info))
            expected = {"manifest.json", "database.dump", *[row["name"] for row in manifest["files"]]}
            if manifest["format"] != 1 or len(members) != len(expected) or {m.name for m in members} != expected:
                raise RecoveryError("Recovery archive does not match its manifest.")
            dump = folder / "database.dump"
            with dump.open("xb") as output:
                source = archive.extractfile("database.dump")
                while chunk := source.read(CHUNK):
                    output.write(chunk)
            if digest(dump) != manifest["database_sha256"]:
                raise RecoveryError("Database dump integrity check failed.")
            for entry in manifest["files"]:
                if entry["type"] not in ("evidence", "legacy"):
                    raise RecoveryError("Unknown recovery file type.")
                # Write each original evidence payload to a fresh safe path, never an archived path.
                expected_name = ("evidence/" if entry["type"] == "evidence" else "legacy/") + str(uuid.UUID(entry["id"]))
                if entry["name"] != expected_name or archive.getmember(entry["name"]).size != entry["size"] or entry["size"] > settings.EVIDENCE_MAX_BYTES:
                    raise RecoveryError("Invalid evidence manifest entry.")
                restored = folder / (entry["type"] + "-" + entry["id"])
                restored.write_bytes(archive.extractfile(entry["name"]).read(settings.EVIDENCE_MAX_BYTES + 1))
                if restored.stat().st_size != entry["size"] or digest(restored) != entry["sha256"]:
                    raise RecoveryError("Evidence restore integrity check failed.")
        try:
            with psycopg.connect(**db_parameters("postgres"), autocommit=True) as admin:
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
                created = True
            pg_tool("pg_restore", name, "--dbname=" + name, "--no-owner", "--no-acl", "--exit-on-error", dump)
            with psycopg.connect(**db_parameters(name)) as restored_db:
                if fingerprints(restored_db) != manifest["tables"]:
                    raise RecoveryError("Restored database records do not match the exported snapshot.")
                rows = restored_db.execute("SELECT id::text, sha256, byte_size FROM evidence_evidenceversion").fetchall()
                references = {entry["id"]: (entry["sha256"], entry["size"]) for entry in manifest["files"] if entry["type"] == "evidence"}
                if {row[0]: (row[1], row[2]) for row in rows} != references:
                    raise RecoveryError("Restored evidence references do not match the recovered files.")
        finally:
            if created:
                with psycopg.connect(**db_parameters("postgres"), autocommit=True) as admin:
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        result = RecoveryDrill.objects.create(bundle_sha256=digest(bundle), database_tables=len(manifest["tables"]), evidence_files=len(manifest["files"]), elapsed_seconds=time.monotonic() - started)
    return result
