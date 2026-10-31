import ast

path = "main.py"
with open(path) as f:
    content = f.read()

old1 = '''    def _run_spot_volume_eod():
        try:
            from services.kite_auth import get_kite_client
            from utils.db import get_supabase
            from api.spot_volume_scanner import append_todays_spot_bar
            append_todays_spot_bar(get_supabase(), get_kite_client(), list(STOCK_NSE_MAP.keys()))
        except Exception as e:
            print(f"[SPOT_VOL] EOD job failed: {e}")'''

new1 = '''    def _run_spot_volume_eod():
        try:
            from utils.db import get_supabase
            from api.spot_volume_scanner import append_todays_spot_bar
            try:
                from services.kite_auth import get_historical_kite_client
                kite = get_historical_kite_client()
                print("[SPOT_VOL] Using Trading Bot historical client for EOD bar")
            except Exception as e:
                print(f"[SPOT_VOL] Historical client unavailable ({e}) -- falling back to main session")
                from services.kite_auth import get_kite_client
                kite = get_kite_client()
            append_todays_spot_bar(get_supabase(), kite, list(STOCK_NSE_MAP.keys()))
        except Exception as e:
            print(f"[SPOT_VOL] EOD job failed: {e}")'''

assert old1 in content, "spot_volume_eod block not found"
content = content.replace(old1, new1)

old2 = '''        try:
            from services.kite_auth import get_kite_client
            from utils.db import get_supabase
            from services.sector_index_capture import append_todays_sector_index_bar
            append_todays_sector_index_bar(get_supabase(), get_kite_client())
        except Exception as e:
            print(f"[SECTOR_IDX] EOD job failed: {e}")'''

new2 = '''        try:
            from utils.db import get_supabase
            from services.sector_index_capture import append_todays_sector_index_bar
            try:
                from services.kite_auth import get_historical_kite_client
                kite = get_historical_kite_client()
                print("[SECTOR_IDX] Using Trading Bot historical client for EOD bar")
            except Exception as e:
                print(f"[SECTOR_IDX] Historical client unavailable ({e}) -- falling back to main session")
                from services.kite_auth import get_kite_client
                kite = get_kite_client()
            append_todays_sector_index_bar(get_supabase(), kite)
        except Exception as e:
            print(f"[SECTOR_IDX] EOD job failed: {e}")'''

assert old2 in content, "sector_index_eod block not found"
content = content.replace(old2, new2)

with open(path.replace(".py", "_new.py"), "w") as f:
    f.write(content)

ast.parse(content)
print("OK - syntax valid")
