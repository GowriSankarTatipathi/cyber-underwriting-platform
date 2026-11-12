# Cyber insurance underwriting pilot

This Phase 1 Django prototype uses one fictional insurer, one commercial cyber product, one evidence checklist, and a sample renewal. It is intended for local demonstrations with fictional data.

## Run locally

Use Python 3.10 or newer. From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py runserver 127.0.0.1:8000
```

On Windows, activate with `.venv\Scripts\activate` from Command Prompt and use `py` to create the environment if `python3` is unavailable. Open http://127.0.0.1:8000/.

## Walk through the case

1. Open **Initial restore**. The test took 420 minutes against a 240-minute target. Data age is within the limit and functional checks passed. The recovery-time requirement still fails.
2. Open **Verified retest**. The retest took 180 minutes, data age was 25 minutes, and functional checks passed. Both targets are met.
3. Review the checklist. The access review remains missing. Discuss what to request and what would support the insurer's decision.
4. Edit `pilot/data/pilot.json` to try another fictional checklist, evidence requirement, or recovery target. Minutes are used throughout. Set an unavailable test measurement to JSON `null` to exercise the missing-evidence path.

The demo is read-only in the browser. The checklist statuses and source labels are sample data. The restore outcome is calculated from the measurements and targets. Django runs locally without database setup. Phase 2 introduces authenticated workspaces, PostgreSQL, and case permissions. Uploads, persistent decisions, and live AI processing belong to later phases.

See [the pilot workshop guide](PILOT.md) for the decisions to agree with an insurer.
