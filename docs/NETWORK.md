# Network requirements

`ingest.py` calls `data.sec.gov` directly -- real HTTP requests to SEC's live XBRL API,
no key required, no mocking. Two things SEC requires of every caller:

1. **A descriptive `User-Agent` header** identifying who's asking -- not a security
   measure, just SEC's own API etiquette. Set it before running anything:

   ```bash
   export TRELLIS_USER_AGENT="YourApp/0.1 your.email@example.com"
   ```

   `ingest.py` raises immediately if this isn't set, rather than sending an
   unidentified request and getting a confusing failure downstream.

2. **A reasonable request rate** -- SEC's own guidance is <=10 requests/second.
   `ingest.py` paces itself well under that, and every request goes through a
   retry-with-backoff session (`make_session()`), since a pull for one company can be
   30-60+ individual requests and a single dropped connection shouldn't fail the whole
   run.

None of this is optional or mockable if you want real results -- the point of this
project is that every number traces back to an actual filing, which means actually
fetching from SEC. If you're running in a network-sandboxed environment (no route to
`data.sec.gov`), the test suite still runs fully offline against synthetic fixtures
shaped like real API responses (`tests/`), but `scripts/run_forecast.py` needs real
network access to do anything.
