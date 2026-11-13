"""Generate disposable, local-only CI files; refuse to overwrite operator files."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import secrets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def prepare(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    files = {
        root / ".env.staging": ("# Disposable CI configuration. No external service credentials.\n"
            f"DJANGO_SECRET_KEY={secrets.token_urlsafe(48)}\nDB_PASSWORD={secrets.token_urlsafe(32)}\n"
            "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\nDJANGO_CSRF_TRUSTED_ORIGINS=https://localhost:8443\n"
            "LOCAL_PASSWORD_ONLY=false\nEVIDENCE_S3_BUCKET=ci-placeholder-no-live-bucket\n"
            "EVIDENCE_STORAGE_BACKEND=s3\nEVIDENCE_SCAN_MODE=external\n"
            "EVIDENCE_SCAN_COMMAND=[\"clamscan\",\"--no-summary\"]\n").encode(),
        root / ".local/staging/secrets/postgres_password": secrets.token_urlsafe(32).encode(),
        root / ".local/staging/tls/server.crt": cert.public_bytes(serialization.Encoding.PEM),
        root / ".local/staging/tls/server.key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    }
    if any(path.exists() for path in files):
        raise FileExistsError("CI staging files already exist; nothing was overwritten.")
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1])
    print("Disposable CI files prepared. No cloud credentials or real partner configuration.")
