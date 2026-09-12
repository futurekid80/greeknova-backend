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
