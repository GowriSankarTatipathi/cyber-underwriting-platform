"""Run this project's private Redis broker over a Unix socket, without a TCP port."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
SOCKET = LOCAL / "run" / "redis.sock"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    import redis
    client = redis.Redis(unix_socket_path=str(SOCKET), socket_connect_timeout=1, socket_timeout=1)
    try:
        running = bool(client.ping())
    except redis.RedisError:
        running = False
    if args.action == "status":
        print("Project queue is running." if running else "Project queue is stopped.")
        return
    if args.action == "stop":
        if running:
            client.shutdown(save=True)
        print("Project queue is stopped.")
        return
    if running:
        print("Project queue is already running.")
        return
    binary = shutil.which("redis-server")
    if not binary:
        sys.exit("Install Redis or set CELERY_BROKER_URL to your private broker. See PHASE5.md.")
    for path in (LOCAL, SOCKET.parent, LOCAL / "redis"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    config = LOCAL / "redis" / "redis.conf"
    def quoted(path):
        return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"'
    config.write_text(f"port 0\nprotected-mode yes\nunixsocket {quoted(SOCKET)}\nunixsocketperm 700\ndaemonize yes\ndir {quoted(LOCAL / 'redis')}\npidfile {quoted(LOCAL / 'redis/redis.pid')}\nlogfile {quoted(LOCAL / 'redis/redis.log')}\nappendonly yes\nsave 60 1\n")
    config.chmod(0o600)
    subprocess.run([binary, str(config)], check=True)
    for _ in range(30):
        try:
            if client.ping():
                print("Project queue is ready. Start: celery -A pilot worker --pool=solo --concurrency=1 --loglevel=INFO")
                return
        except redis.RedisError:
            time.sleep(0.1)
    sys.exit("Redis did not start. Inspect .local/redis/redis.log.")


if __name__ == "__main__":
    main()
