"""Probe the local web process while using an explicitly allowed Host header."""
import os
from urllib.request import Request, urlopen


def check():
    hosts = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",") if host.strip()]
    if not hosts or hosts[0] == "*":
        raise RuntimeError("An explicit allowed host is required for readiness checks.")
    # The destination remains loopback even when staging allows only its domain.
    request = Request("http://127.0.0.1:8000/health/ready/", headers={"Host": hosts[0]})
    with urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("The local web process is not ready.")


if __name__ == "__main__":
    check()
