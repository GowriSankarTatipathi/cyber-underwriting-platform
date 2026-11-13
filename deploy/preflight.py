"""Offline staging prerequisite checks. Does not start services or print secrets."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key
from dotenv import dotenv_values


def inspect_files(root, environment):
    problems = []
    env_path = Path(environment)
    if not env_path.is_file():
        return ["Create the private staging environment file from deploy/staging.env.example."]
    values = dotenv_values(env_path, interpolate=False)
    if stat.S_IMODE(env_path.stat().st_mode) & 0o077:
        problems.append("Restrict the staging environment file to its owner (chmod 600).")
    for field, length in (("DJANGO_SECRET_KEY", 50), ("DB_PASSWORD", 24)):
        value = values.get(field) or ""
        if len(value) < length or "replace-with" in value:
            problems.append(f"Configure a private random {field} of at least {length} characters.")
    if values.get("POSTGRES_PASSWORD") or values.get("POSTGRES_PASSWORD_FILE"):
        problems.append("Remove PostgreSQL administrator credentials from the application environment file.")
    if (values.get("LOCAL_PASSWORD_ONLY") or "false").lower() != "false":
        problems.append("Set LOCAL_PASSWORD_ONLY=false for staging.")
    hosts = [h.strip() for h in (values.get("DJANGO_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    if not hosts or any("*" in h or "/" in h for h in hosts):
        problems.append("Configure explicit allowed hostnames without wildcards or URL paths.")
    origins = (values.get("DJANGO_CSRF_TRUSTED_ORIGINS") or "").split(",")
    try:
        if any(urlsplit(o).scheme != "https" or urlsplit(o).hostname not in hosts or urlsplit(o).path not in ("", "/") for o in origins):
            problems.append("Use HTTPS CSRF origins matching the staging hostnames, including the public port.")
    except ValueError:
        problems.append("Correct the staging CSRF origin URLs.")
    bucket = values.get("EVIDENCE_S3_BUCKET") or ""
    if not bucket or bucket.startswith("your-"):
        problems.append("Configure the private evidence bucket name.")
    secret = root / ".local/staging/secrets/postgres_password"
    if not secret.is_file():
        problems.append("Create .local/staging/secrets/postgres_password for PostgreSQL only.")
    else:
        password = secret.read_text().strip()
        if len(password) < 24 or password == values.get("DB_PASSWORD"):
            problems.append("Use a separate random database administrator password of at least 24 characters.")
        if stat.S_IMODE(secret.stat().st_mode) & 0o077:
            problems.append("Restrict the database administrator password file (chmod 600).")
    certificate, private_key = (root / ".local/staging/tls" / name for name in ("server.crt", "server.key"))
    if not certificate.is_file() or not private_key.is_file():
        problems.append("Provide server.crt and server.key in .local/staging/tls/.")
    else:
        if stat.S_IMODE(private_key.stat().st_mode) & 0o077:
            problems.append("Restrict the TLS private key (chmod 600).")
        try:
            cert = x509.load_pem_x509_certificate(certificate.read_bytes())
            key = load_pem_private_key(private_key.read_bytes(), password=None)
            public = lambda k: k.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            if public(cert.public_key()) != public(key.public_key()):
                problems.append("The TLS certificate and private key do not match.")
            if not cert.not_valid_before_utc <= datetime.now(timezone.utc) < cert.not_valid_after_utc:
                problems.append("The TLS certificate is expired or not yet valid.")
        except (ValueError, TypeError):
            problems.append("Use a valid PEM certificate and unencrypted PEM server key protected by filesystem permissions.")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.staging")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        problems = inspect_files(root, root / args.env_file)
    except OSError:
        problems = ["Unable to read a required staging file. Check file ownership and permissions."]
    if problems:
        for problem in problems:
            print("- " + problem, file=sys.stderr)
        return 1
    print("Local staging prerequisites passed. Certificate trust, external services and container startup still require verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
