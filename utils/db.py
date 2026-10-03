import os
from supabase import create_client, ClientOptions
import httpx
from dotenv import load_dotenv

load_dotenv()

# BUG FIX (Sep 12 2026): supabase-py's postgrest client hardcodes
# httpx.Client(http2=True) with no way to opt out except passing our own
# httpx client through ClientOptions. That http2 connection was resetting
# constantly (httpx.RemoteProtocolError: ConnectionTerminated) -- hitting
# every endpoint that talks to Supabase (uoa, cpr-scanner, oi-pulse,
# index-data...), consistently, not just occasionally, even on trivial
# queries against small tables. Forcing HTTP/1.1 avoids that failure mode
# entirely -- plain Postgres access (outside PostgREST/http2) was never
# affected, which is what pointed at the http2 transport as the cause.
_http_client = httpx.Client(http2=False)


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    return create_client(url, key, options=ClientOptions(httpx_client=_http_client))


def get_supabase_admin():
    """
    Service-role client. Needed for anything that calls supabase.auth.admin.*
    (e.g. generating a login link server-side) -- the normal anon/public
    SUPABASE_KEY cannot do this, even though it's enough for all our regular
    table reads/writes under RLS.
    Requires SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY as a fallback
    name) to be set in the environment. Raises a clear error if missing,
    rather than silently falling back to the anon key.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not set -- required for admin auth operations."
        )
    return create_client(url, key, options=ClientOptions(httpx_client=_http_client))
