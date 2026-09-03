# Running the live ingestion

`src/trellis/ingest.py` calls `data.sec.gov` directly. That host isn't reachable from
the sandboxed environment this project was scaffolded in, so `fetch_all()` has been
built and tested against a synthetic fixture (`tests/test_ingest.py`) that mirrors the
real API's shape, not run against Nike's live data yet.

To run it for real:

```bash
export TRELLIS_USER_AGENT="Trellis/0.1 your.email@example.com"  # SEC requires this
python3 -c "from trellis.ingest import fetch_all; print(fetch_all(320187))"
```

No API key needed. SEC's own guidance: <=10 requests/second; `ingest.py` already
paces itself well under that.
