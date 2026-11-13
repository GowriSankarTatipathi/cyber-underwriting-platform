import subprocess
import sys

for command in (
    ["check_staging"],
    ["check", "--deploy", "--fail-level", "WARNING"],
    ["migrate", "--noinput"],
    ["collectstatic", "--noinput"],
):
    subprocess.run([sys.executable, "manage.py", *command], check=True)
