"""Create a project-only PostgreSQL cluster; never touch Homebrew's service."""
import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
DATA = LOCAL / "postgres"
SOCKET = LOCAL / "run"
ENV = ROOT / ".env"


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    for executable in ("initdb", "pg_ctl"):
        if not shutil.which(executable):
            sys.exit(f"{executable} is not on PATH. Install PostgreSQL or configure an existing database using .env.example.")
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    LOCAL.chmod(0o700)
    SOCKET.mkdir(exist_ok=True, mode=0o700)
    if args.action in ("stop", "status"):
        run("pg_ctl", "-D", DATA, args.action, *(["-m", "fast"] if args.action == "stop" else []))
        return
    if not ENV.exists():
        content = (
            f"DJANGO_SECRET_KEY={secrets.token_urlsafe(48)}\nDJANGO_DEBUG=true\n"
            f"DB_NAME=cyber_underwriting\nDB_USER=cyber\nDB_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"DB_HOST={SOCKET}\nDB_PORT=55432\n"
        )
        fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    from dotenv import dotenv_values
    config = dotenv_values(ENV)
    if config.get("DB_HOST") != str(SOCKET) or config.get("DB_USER") != "cyber" or config.get("DB_PORT") != "55432":
        sys.exit(".env points to another database. Leaving it unchanged; start that database separately.")
    if not config.get("DB_PASSWORD"):
        sys.exit("DB_PASSWORD must be set in .env.")
    if not (DATA / "PG_VERSION").exists():
        password_file = LOCAL / "pg-init-password"
        fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(config["DB_PASSWORD"])
            run("initdb", "-D", DATA, "-U", "cyber", "--auth=scram-sha-256", f"--pwfile={password_file}", "--encoding=UTF8", "--locale=C")
        finally:
            password_file.unlink(missing_ok=True)
        with (DATA / "postgresql.conf").open("a") as stream:
            stream.write(f"\nlisten_addresses = ''\nport = 55432\nunix_socket_directories = '{SOCKET}'\n")
    running = subprocess.run(["pg_ctl", "-D", str(DATA), "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not running:
        run("pg_ctl", "-D", DATA, "-l", LOCAL / "postgres.log", "-w", "start")
    import psycopg
    from psycopg import sql
    with psycopg.connect(dbname="postgres", user="cyber", password=config["DB_PASSWORD"], host=str(SOCKET), port=55432, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config["DB_NAME"],)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(config["DB_NAME"])))
    print("Project PostgreSQL is ready. Run: python manage.py migrate")


if __name__ == "__main__":
    main()
