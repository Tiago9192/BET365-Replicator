from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import json
import os
import time
import concurrent.futures
from urllib.parse import urlparse
from datetime import datetime

app = Flask(__name__)
CORS(app)

QRSOLVER_BASE  = "https://qrsolver.com"
MAX_IP_RETRIES = 15

# ══════════════════════════════════════════════════════════════════
# DATABASE — PostgreSQL via Railway
# ══════════════════════════════════════════════════════════════════

import pg8000.native
from urllib.parse import urlparse as _pg_urlparse

def get_db():
    # Try DATABASE_PUBLIC_URL first (external), then DATABASE_URL (internal)
    db_url = os.environ.get("DATABASE_PUBLIC_URL", "") or os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise Exception("DATABASE_URL not configured")
    p = _pg_urlparse(db_url)
    return pg8000.native.Connection(
        host=p.hostname,
        port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        ssl_context=True
    )

# ── Race Queue persistence ────────────────────────────────────────────────────
def load_race_queue():
    """Load race queue from PostgreSQL."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = :key", {"key": "race_queue"})
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            import json as _json
            return _json.loads(row[0])
    except Exception as e:
        print(f"load_race_queue error: {e}")
    return {}

def save_race_queue(queue):
    """Save race queue to PostgreSQL."""
    try:
        import json as _json
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO settings (key, value) VALUES (:key, :value)
            ON CONFLICT (key) DO UPDATE SET value = :value
        """, {"key": "race_queue", "value": _json.dumps(queue)})
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"save_race_queue error: {e}")


def db_exec(sql, *params):
    """Execute SQL and return rows as list of dicts."""
    conn = get_db()
    try:
        rows = conn.run(sql, *params)
        cols = [c["name"] for c in conn.columns] if conn.columns else []
        return [dict(zip(cols, row)) for row in (rows or [])]
    finally:
        conn.close()

def db_run(sql, *params):
    """Execute SQL without returning rows."""
    conn = get_db()
    try:
        conn.run(sql, *params)
    finally:
        conn.close()

def init_db():
    """Create tables if they don't exist."""
    db_run("""
        CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    db_run("""
        CREATE TABLE IF NOT EXISTS ip_history (
            ip TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    db_run("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

def load_accounts():
    try:
        rows = db_exec("SELECT data FROM accounts")
        accounts = [json.loads(row["data"]) for row in rows]
        return sorted(accounts, key=lambda a: a.get("id", 0))
    except Exception as e:
        print(f"ERROR load_accounts: {e}")
        import traceback
        traceback.print_exc()
        return []

def save_accounts(accounts):
    try:
        conn = get_db()
        try:
            conn.run("DELETE FROM accounts")
            for a in accounts:
                conn.run("INSERT INTO accounts (data) VALUES (:data)", data=json.dumps(a))
            print(f"DEBUG save_accounts: saved {len(accounts)} accounts")
        finally:
            conn.close()
    except Exception as e:
        print(f"ERROR save_accounts: {e}")
        import traceback
        traceback.print_exc()

# ── IP History ────────────────────────────────────────────────────────────────

def load_ip_history():
    try:
        rows = db_exec("SELECT ip, data FROM ip_history")
        return {row["ip"]: json.loads(row["data"]) for row in rows}
    except Exception:
        return {}

def save_ip_history(history):
    try:
        conn = get_db()
        try:
            conn.run("DELETE FROM ip_history")
            for ip, data in history.items():
                conn.run("INSERT INTO ip_history (ip, data) VALUES (:ip, :data)", ip=ip, data=json.dumps(data))
        finally:
            conn.close()
    except Exception as e:
        print(f"Error saving IP history: {e}")

def record_ip(ip, account_id, account_name):
    """Add or update an IP entry in the permanent history."""
    history = load_ip_history()
    now = datetime.utcnow().isoformat()
    if ip in history:
        history[ip]["last_seen"] = now
        history[ip]["times_used"] = history[ip].get("times_used", 1) + 1
        # Update account ownership to current
        history[ip]["account_id"]   = account_id
        history[ip]["account_name"] = account_name
    else:
        history[ip] = {
            "account_id":   account_id,
            "account_name": account_name,
            "first_seen":   now,
            "last_seen":    now,
            "times_used":   1
        }
    save_ip_history(history)

def ip_used_by_other_account(ip, my_account_id):
    """
    Returns (True, owner_name) if this IP has been used by a DIFFERENT account.
    Returns (False, None) if IP is free or belongs to my own account.
    """
    history = load_ip_history()
    if ip not in history:
        return False, None
    entry = history[ip]
    if entry["account_id"] == my_account_id:
        return False, None   # It's my own IP, OK to reuse
    return True, entry["account_name"]

def clear_ip_history():
    save_ip_history({})

# ══════════════════════════════════════════════════════════════════
# PROXY / IP HELPERS
# ══════════════════════════════════════════════════════════════════

def get_headers(api_key):
    return {"Authorization": api_key.strip(), "Content-Type": "application/json"}

def detect_ip_via_proxy(proxy_url):
    """Detect real outbound IP through the proxy. Tries 3 services."""
    if not proxy_url:
        return None
    proxies = {"http": proxy_url, "https": proxy_url}
    for url in [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip",
        "https://api4.my-ip.io/ip.json"
    ]:
        try:
            r = requests.get(url, proxies=proxies, timeout=12)
            if r.status_code == 200:
                data = r.json()
                ip = data.get("ip") or data.get("origin","").split(",")[0].strip()
                if ip:
                    return ip.strip()
        except Exception:
            continue
    return None

def force_proxy_rotation(proxy_url):
    """
    For rotating/sticky mobile proxies, trigger a new IP by:
    1. Waiting a moment (some providers rotate on reconnect)
    2. Returning the same URL — the provider will give a new IP on next connection
    Most mobile proxy providers rotate automatically on each new TCP session.
    """
    time.sleep(1.5)  # Brief pause so provider assigns a fresh IP
    return proxy_url  # Same URL, new connection = new IP from the pool

# ══════════════════════════════════════════════════════════════════
# QRSOLVER API
# ══════════════════════════════════════════════════════════════════

def qrsolver_request(method, endpoint, api_key, body=None, params=None):
    url = f"{QRSOLVER_BASE}{endpoint}"
    headers = get_headers(api_key)
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
        elif method == "POST":
            r = requests.post(url, headers=headers, json=body, timeout=60, verify=False)
        elif method == "PATCH":
            r = requests.patch(url, headers=headers, json=body, timeout=30, verify=False)
        elif method == "DELETE":
            r = requests.delete(url, headers=headers, timeout=30, verify=False)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        return 0, {"error": str(e)}

def parse_bet365_url(url):
    try:
        parsed = urlparse(url)
        fragment = parsed.fragment
        if not fragment:
            return None, "No se encontró el hash en la URL"
        # Convert slashes to # — keep leading # as the API expects it
        # Doc example: #AC#B1#C1#D1002#G938#J20#Q1#F^3#
        pd = fragment.replace("/", "#")
        # Ensure it starts with # (fragment starts with / which becomes #)
        if not pd.startswith("#"):
            pd = "#" + pd
        # Remove trailing # if present
        if pd.endswith("#"):
            pd = pd[:-1]
        return pd, None
    except Exception as e:
        return None, str(e)

# ══════════════════════════════════════════════════════════════════
# CORE: LOGIN WITH IP DEDUPLICATION
# ══════════════════════════════════════════════════════════════════

def login_account_safe(account):
    """
    1. Detect current IP through proxy
    2. Check against IP history — if another account already used it → force rotation
    3. Keep rotating until a clean IP is found
    4. Record the clean IP in history permanently
    5. Create session + login on Bet365
    """
    account_id   = account["id"]
    account_name = account["name"]
    api_key      = account["api_key"]
    proxy        = account.get("proxy", "")
    ip_log       = []
    detected_ip  = None

    # ── Phase 1: Find a unique IP ──────────────────────────────────────────
    for attempt in range(1, MAX_IP_RETRIES + 1):

        ip = detect_ip_via_proxy(proxy)
        entry = {
            "attempt": attempt,
            "ip": ip or "no_detectada",
            "ts": datetime.utcnow().strftime("%H:%M:%S")
        }

        if not ip:
            entry["status"] = "⚠ no detectada"
            ip_log.append(entry)
            # If we can't detect the IP, proceed anyway (no proxy or detection failed)
            break

        used_by_other, owner_name = ip_used_by_other_account(ip, account_id)

        if not used_by_other:
            # ✓ IP is clean — record it and proceed
            entry["status"] = f"✓ libre — asignada a {account_name}"
            ip_log.append(entry)
            record_ip(ip, account_id, account_name)
            detected_ip = ip
            break
        else:
            # ✗ IP was used by another account — force rotation
            entry["status"] = f"✗ ya usada por {owner_name} — rotando..."
            ip_log.append(entry)
            proxy = force_proxy_rotation(proxy)  # Triggers new IP on next connect

    if not detected_ip and ip_log and ip_log[-1]["ip"] != "no_detectada":
        # Ran out of retries — record what we have but warn
        detected_ip = ip_log[-1]["ip"]
        record_ip(detected_ip, account_id, account_name)
        ip_log.append({
            "attempt": MAX_IP_RETRIES + 1,
            "ip": detected_ip,
            "ts": datetime.utcnow().strftime("%H:%M:%S"),
            "status": "⚠ agotados reintentos — usando esta IP"
        })

    # ── Phase 2: Create session on QRSolver ───────────────────────────────
    domain = account.get("domain", "https://www.bet365.com/")
    body = {
        "domain": domain,
        "username": account["username"],
        "password": account["password"],
        "country_code": account["country_code"],
        "keepalive": True
    }
    if account.get("proxy"):
        body["proxy"] = account["proxy"]

    status, resp = qrsolver_request("POST", "/api/placebet/create/", api_key, body)
    if status != 200 or "session_id" not in resp:
        return {
            "id": account_id, "name": account_name,
            "success": False,
            "error": f"Error creando sesión: {resp}",
            "ip": detected_ip,
            "ip_log": ip_log
        }

    session_id = resp["session_id"]

    # ── Phase 3: Login ─────────────────────────────────────────────────────
    status2, resp2 = qrsolver_request(
        "POST", f"/api/placebet/session/{session_id}/login/", api_key, body
    )

    success = status2 == 200
    return {
        "id":         account_id,
        "name":       account_name,
        "success":    success,
        "session_id": session_id if success else None,
        "ip":         detected_ip,
        "ip_log":     ip_log,
        "response":   resp2
    }

# ══════════════════════════════════════════════════════════════════
# ROUTES — ACCOUNTS
# ══════════════════════════════════════════════════════════════════

@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    return jsonify(load_accounts())

@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.json
    for field in ["name","username","password","country_code","api_key"]:
        if not data.get(field):
            return jsonify({"error": f"Campo requerido: {field}"}), 400
    accounts = load_accounts()
    account = {
        "id":           max((a["id"] for a in accounts), default=0) + 1,
        "name":         data["name"],
        "username":     data["username"],
        "password":     data["password"],
        "country_code": data["country_code"],
        "domain":       data.get("domain", "https://www.bet365.com/"),
        "proxy":        data.get("proxy",""),
        "api_key":      data["api_key"].strip(),
        "session_id":   None,
        "status":       "disconnected",
        "current_ip":   None,
        "ip_log":       []
    }
    accounts.append(account)
    save_accounts(accounts)
    return jsonify({"success": True, "account": account})

@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    accounts = [a for a in load_accounts() if a["id"] != account_id]
    save_accounts(accounts)
    return jsonify({"success": True})

@app.route("/api/accounts/<int:account_id>", methods=["PATCH"])
def update_account(account_id):
    data     = request.json
    accounts = load_accounts()
    account  = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": "Cuenta no encontrada"}), 404

    # Update allowed fields
    for field in ["name","username","password","country_code","domain","proxy","api_key"]:
        if field in data:
            account[field] = data[field].strip() if isinstance(data[field], str) else data[field]

    save_accounts(accounts)
    return jsonify({"success": True})

# ── Login single account ───────────────────────────────────────────────────────
@app.route("/api/accounts/<int:account_id>/login", methods=["POST"])
def login_one(account_id):
    accounts = load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        return jsonify({"error": "Cuenta no encontrada"}), 404

    result = login_account_safe(account)

    for a in accounts:
        if a["id"] == account_id:
            a["session_id"]    = result.get("session_id")
            a["status"]        = "connected" if result["success"] else "error"
            a["current_ip"]    = result.get("ip")
            a["ip_log"]        = result.get("ip_log", [])
            a["last_activity"] = datetime.utcnow().isoformat()
    save_accounts(accounts)
    return jsonify(result)

# ── Login ALL ──────────────────────────────────────────────────────────────────
# Must be sequential so each account registers its IP before the next one checks
@app.route("/api/login-all", methods=["POST"])
def login_all():
    accounts = load_accounts()
    results  = []

    for account in accounts:
        result = login_account_safe(account)
        results.append(result)
        # Update account in file immediately so the next account sees this IP
        fresh = load_accounts()
        for a in fresh:
            if a["id"] == account["id"]:
                a["session_id"] = result.get("session_id")
                a["status"]     = "connected" if result["success"] else "error"
                a["current_ip"] = result.get("ip")
                a["ip_log"]     = result.get("ip_log", [])
        save_accounts(fresh)

    connected = sum(1 for r in results if r["success"])
    return jsonify({
        "results": results,
        "summary": {
            "total":     len(results),
            "connected": connected,
            "failed":    len(results) - connected
        }
    })

# ── Logout ─────────────────────────────────────────────────────────────────────
@app.route("/api/accounts/<int:account_id>/logout", methods=["POST"])
def logout_account(account_id):
    accounts = load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account or not account.get("session_id"):
        return jsonify({"error": "Sin sesión activa"}), 400
    # First logout, then delete session to fully free the slot
    qrsolver_request("POST", f"/api/placebet/session/{account['session_id']}/logout/", account["api_key"])
    qrsolver_request("DELETE", f"/api/placebet/session/{account['session_id']}/", account["api_key"])
    for a in accounts:
        if a["id"] == account_id:
            a["session_id"] = None
            a["status"]     = "disconnected"
    save_accounts(accounts)
    return jsonify({"success": True})

# ── Balance ────────────────────────────────────────────────────────────────────
@app.route("/api/accounts/<int:account_id>/balance", methods=["GET"])
def get_balance(account_id):
    accounts = load_accounts()
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account or not account.get("session_id"):
        return jsonify({"error": "Sin sesión activa"}), 400
    _, resp = qrsolver_request("GET", f"/api/placebet/session/{account['session_id']}/balance/", account["api_key"])
    return jsonify(resp)

# ── Keepalive ──────────────────────────────────────────────────────────────────
@app.route("/api/keepalive", methods=["POST"])
def keepalive_all():
    accounts = load_accounts()
    results = []
    for a in accounts:
        if a.get("session_id"):
            _, resp = qrsolver_request("POST", f"/api/placebet/session/{a['session_id']}/keepalive/", a["api_key"])
            results.append({"name": a["name"], "alive": resp.get("alive", False)})
    return jsonify({"results": results})

# ══════════════════════════════════════════════════════════════════
# ROUTES — IP HISTORY
# ══════════════════════════════════════════════════════════════════

@app.route("/api/ip-history", methods=["GET"])
def get_ip_history():
    history  = load_ip_history()
    accounts = load_accounts()

    # Build per-account summary
    account_ips = {}
    for ip, data in history.items():
        aid = data["account_id"]
        if aid not in account_ips:
            account_ips[aid] = []
        account_ips[aid].append({
            "ip":         ip,
            "first_seen": data["first_seen"][:16].replace("T"," "),
            "last_seen":  data["last_seen"][:16].replace("T"," "),
            "times_used": data["times_used"]
        })

    result = []
    for a in accounts:
        result.append({
            "id":         a["id"],
            "name":       a["name"],
            "status":     a["status"],
            "current_ip": a.get("current_ip"),
            "ip_log":     a.get("ip_log", []),
            "all_ips":    sorted(account_ips.get(a["id"], []), key=lambda x: x["last_seen"], reverse=True)
        })

    # Detect any current conflicts (same IP assigned to 2+ accounts right now)
    current_ips = [(a["name"], a.get("current_ip")) for a in accounts if a.get("current_ip")]
    seen = {}
    conflicts = []
    for name, ip in current_ips:
        if ip in seen:
            conflicts.append({"ip": ip, "accounts": [seen[ip], name]})
        else:
            seen[ip] = name

    return jsonify({
        "accounts": result,
        "conflicts": conflicts,
        "total_ips_recorded": len(history)
    })

@app.route("/api/ip-history/clear", methods=["POST"])
def clear_history():
    clear_ip_history()
    return jsonify({"success": True, "message": "Historial de IPs borrado"})

@app.route("/api/ip-history/delete-ip", methods=["POST"])
def delete_single_ip():
    ip = request.json.get("ip")
    if not ip:
        return jsonify({"error": "IP requerida"}), 400
    history = load_ip_history()
    if ip in history:
        del history[ip]
        save_ip_history(history)
        return jsonify({"success": True})
    return jsonify({"error": "IP no encontrada"}), 404


# ══════════════════════════════════════════════════════════════════
# HORSE RACING — LOAD RUNNERS FROM URL
# ══════════════════════════════════════════════════════════════════

def odd_to_decimal(odd_str):
    """Convert fractional odd (e.g. '9/2') or decimal string to float."""
    if not odd_str:
        return None
    odd_str = str(odd_str).strip()
    try:
        if "/" in odd_str:
            num, den = odd_str.split("/")
            return round(int(num) / int(den) + 1, 3)
        return round(float(odd_str), 3)
    except Exception:
        return None

def parse_prematch_runners(raw):
    """
    Parse bet365 pipe-delimited stream to extract horse runners.
    Format: F|CL;...|EV;...|SE;ID=123;NA=HorseName;OD=9/2;...
    SE = Selection record, PA = Participant record
    """
    import re
    runners = []
    if not raw:
        return runners

    text = raw if isinstance(raw, str) else json.dumps(raw)
    seen_ids = set()

    # Method 1: Parse bet365 pipe-delimited format
    # Records separated by | with fields separated by ;
    records = text.split('|')
    for rec in records:
        rec_type = rec.split(';')[0] if ';' in rec else rec[:4]
        if rec_type not in ('SE', 'PA', 'SL', 'OC', 'TL'):
            continue

        fields = {}
        for f in rec.split(';'):
            if '=' in f:
                k, _, v = f.partition('=')
                fields[k.strip()] = v.strip()

        sel_id   = fields.get('ID', '')
        name     = fields.get('NA', '') or fields.get('NM', '')
        odd      = fields.get('FW', '') or fields.get('OD', '') or fields.get('HA', '')
        prog_num = fields.get('PN', '') or fields.get('SN', '')
        fi_field = fields.get('FI', '')

        # Skip records without a real horse name
        if not name or name.isdigit() or len(name) <= 1:
            continue

        # Skip fake entries like Favourite, Favorito, 2nd Favourite, etc.
        name_lower = name.lower().strip()
        skip_names = [
            'favourite', 'favorite', '2nd favourite', '2nd favorite',
            'third favourite', 'field', 'unnamed', 'unnamed favourite',
            'favorito', 'favorita', '2º favorito', '2o favorito',
            '2nd favorito', 'segundo favorito', 'tercer favorito',
            'the field', 'field bet'
        ]
        if name_lower in skip_names:
            continue
        # Skip names that are clearly fake betting entries
        if name_lower.startswith(('2º favorit', '2o favorit', '2nd favour', '3rd favour')):
            continue
        # Skip if name is just a number like "99", "10"
        try:
            int(name.strip())
            continue
        except:
            pass

        # Skip if no FI field (not a real runner record)
        if not fi_field:
            continue

        # Skip if no program number (not a real runner)
        if not prog_num:
            continue

        try:
            sel_id_int = int(sel_id)
        except:
            continue  # Must have valid ID

        # Skip if ID looks like it's from EP field (Exacta/Trifecta)
        # Real win selection IDs are in ID= field directly
        if sel_id_int == 0:
            continue

        if sel_id_int not in seen_ids:
            dec = odd_to_decimal(odd) if odd else 0
            seen_ids.add(sel_id_int)
            runners.append({
                "id":       sel_id_int,
                "name":     name,
                "odd_raw":  odd or "SP",
                "odd_dec":  dec or 0,
                "prog_num": prog_num
            })

    # Method 2: Regex for NA=Name patterns with odds
    if not runners:
        p = re.compile(r'NA=([A-Za-z][A-Za-z0-9 \'\-\.]{2,35});[^|]*?(?:OD|OR)=(\d+/\d+|\d+\.\d+)')
        seen = set()
        for m in p.finditer(text):
            name = m.group(1).strip()
            odd  = m.group(2).strip()
            dec  = odd_to_decimal(odd)
            if name and name not in seen:
                seen.add(name)
                runners.append({"id": 0, "name": name, "odd_raw": odd, "odd_dec": dec or 0})

    # Method 3: broad regex fallback
    if not runners:
        p2 = re.compile(r'(\d{7,12})[^\d]([A-Za-z][A-Za-z0-9 \'\-\.]{2,30})[^\d](\d+/\d+)')
        seen2 = set()
        for m in p2.finditer(text):
            name = m.group(2).strip()
            odd  = m.group(3).strip()
            sid  = int(m.group(1))
            dec  = odd_to_decimal(odd)
            if dec and dec > 1.0 and name not in seen2:
                seen2.add(name)
                runners.append({"id": sid, "name": name, "odd_raw": odd, "odd_dec": dec})

    return sorted(runners, key=lambda x: x["odd_dec"]) if runners else runners



# ══════════════════════════════════════════════════════════════════
# HORSE RACING — FETCH DIRECTLY FROM BET365 VIA PROXY
# ══════════════════════════════════════════════════════════════════

@app.route("/api/race/runners", methods=["POST"])
def load_runners():
    import re
    from urllib.parse import urlparse, unquote
    data = request.json
    url  = data.get("url", "")
    if not url:
        return jsonify({"error": "URL requerida"}), 400

    # Extract sport_id and fi from URL
    sm = re.search(r'/B(\d+)/', url)
    fm = re.search(r'/F(\d+)/', url)
    sport_id = int(sm.group(1)) if sm else 73
    fi       = int(fm.group(1)) if fm else 0

    # Build clean PD hash
    fragment = urlparse(url).fragment
    fragment = unquote(fragment)
    fragment = re.sub(r'/X[^/]*/.*$', '/', fragment)
    fragment = re.sub(r'/X[^/]*$', '/', fragment)
    pd = fragment.replace("/", "#")
    if not pd.startswith("#"):
        pd = "#" + pd
    # Ensure trailing # as API requires it
    if not pd.endswith("#"):
        pd = pd + "#"

    # Get any account with api_key (connected or not)
    accounts = load_accounts()
    account  = next((a for a in accounts if a.get("status") == "connected" and a.get("session_id")), None)
    if not account:
        # Try disconnected account - slot should be free
        account = next((a for a in accounts if a.get("api_key")), None)
    if not account:
        return jsonify({"error": "Configura al menos una cuenta con API key"}), 400

    api_key    = account["api_key"]
    session_id = account.get("session_id")
    proxy      = account.get("proxy", "")
    domain     = account.get("domain", "https://www.bet365.com/")

    runners     = []
    raw         = ""
    fetch_error = None

    # Use dedicated guest proxy if configured, otherwise fall back to account proxy
    def encode_proxy(p):
        if not p: return p
        try:
            from urllib.parse import urlparse as _up, quote as _q
            parsed = _up(p)
            if parsed.password:
                return p.replace(f":{parsed.password}@", f":{_q(parsed.password, safe='')}@")
        except: pass
        return p

    settings        = load_settings()
    guest_proxy     = settings.get("guest_proxy", "").strip()
    proxy_for_guest = guest_proxy if guest_proxy else proxy

    # Create guest session WITHOUT touching the main account session
    try:

        guest_body = {"domain": domain}
        if proxy_for_guest:
            guest_body["proxy"] = encode_proxy(proxy_for_guest)

        gs, gr = qrsolver_request("POST", "/api/placebet/guest/create/", api_key, guest_body)

        if gs == 200 and "session_id" in gr:
            guest_id = gr["session_id"]

            # Get prematch data — response is text/plain streamed from bet365
            url_pm = f"{QRSOLVER_BASE}/api/placebet/guest/{guest_id}/prematch"
            headers_pm = get_headers(api_key)
            try:
                r_pm = requests.get(url_pm, headers=headers_pm, params={"pd": pd}, timeout=30, verify=False)
                raw = r_pm.text
                if not raw:
                    raw = r_pm.content.decode("utf-8", errors="replace")
            except Exception as e_pm:
                raw = json.dumps({"error": str(e_pm)})

            if "invalid" not in raw.lower() and "error" not in raw.lower() and "connecterror" not in raw.lower():
                runners = parse_prematch_runners(raw)
            else:
                fetch_error = f"Prematch error: {raw[:200]}"

            # Close guest session immediately to free the slot
            qrsolver_request("DELETE", f"/api/placebet/session/{guest_id}/", api_key)
        else:
            fetch_error = f"Error creando guest: {gr}"

    except Exception as e:
        fetch_error = str(e)


    # Extract venue and race name from raw stream
    import re as _re
    venue_match    = _re.search(r'N2=([^;|\n]+)', raw) if raw else None
    racenum_match  = _re.search(r'N3=([^;|\n]+)', raw) if raw else None
    venue_name     = venue_match.group(1).strip() if venue_match else ""
    race_num_name  = racenum_match.group(1).strip() if racenum_match else ""
    if venue_name and race_num_name:
        race_name = f"{venue_name} — {race_num_name}"
    elif venue_name:
        race_name = venue_name
    else:
        race_name = ""

    return jsonify({
        "runners":     runners,
        "pd":          pd,
        "sport_id":    sport_id,
        "fi":          fi,
        "race_name":   race_name,
        "fetch_error": fetch_error,
        "raw_sample":  raw[-3000:] if len(raw) > 3000 else raw,
        "proxy_used":  proxy_for_guest[:30] + "..." if proxy_for_guest and len(proxy_for_guest) > 30 else proxy_for_guest
    })

@app.route("/api/placebet", methods=["POST"])
def place_bet_all():
    data         = request.json
    bet365_url   = data.get("url")
    stake        = float(data.get("stake", 10))
    sport_id     = data.get("sport_id", 73)
    fi           = data.get("fi", 0)
    selection_id = data.get("selection_id", 0)
    odd_raw      = data.get("odd", "")
    odd_drop_pct = float(data.get("odd_drop_pct", 10))  # block if drops > X%
    pd_hash      = data.get("pd", "")

    if not bet365_url:
        return jsonify({"error": "URL de Bet365 requerida"}), 400
    if not selection_id:
        return jsonify({"error": "Selecciona un caballo primero"}), 400

    pd, error = parse_bet365_url(bet365_url)
    if error:
        return jsonify({"error": error}), 400

    accounts = load_accounts()
    active   = [a for a in accounts if a.get("session_id") and a.get("status") == "connected"]
    if not active:
        return jsonify({"error": "No hay cuentas conectadas"}), 400

    # Update last activity timestamp
    now = datetime.utcnow().isoformat()
    for a in accounts:
        if a.get("status") == "connected":
            a["last_activity"] = now
    save_accounts(accounts)

    # No odd protection — always fire regardless of odd change
    odd_check = {"checked": False, "blocked": False, "reason": ""}

    # ── Fire on all accounts with random delay ──────────────────────────
    import math as _math, random as _random

    # Get stake units from request (if using bankroll system)
    stake_units = data.get("stake_units", None)

    def get_stake_for_account(account):
        """Calculate stake for account using bankroll or fixed stake."""
        if stake_units is not None:
            account_stake1 = account.get("stake1", 0)
            if account_stake1 > 0:
                raw = stake_units * account_stake1
                return int(_math.ceil(raw))  # round up to next integer
        return stake  # fallback to fixed stake

    def bet_one(account):
        # Random delay 0-3 seconds to simulate human behavior
        delay = _random.uniform(0, 3)
        time.sleep(delay)

        account_stake = get_stake_for_account(account)

        selection = {
            "sport_id":         sport_id,
            "fi":               fi,
            "id":               selection_id,
            "odd":              odd_raw,
            "stake":            account_stake,
            "accept_min_odd":   1.01,
            "accept_max_odd":   1000.0,
            "accept_odd_lines": []
        }

        bet_body = {
            "type":       "singles",
            "selections": [selection],
            "stake":      account_stake
        }
        status, resp = qrsolver_request(
            "POST",
            f"/api/placebet/session/{account['session_id']}/placebet/",
            account["api_key"],
            bet_body
        )
        return {
            "id":      account["id"],
            "name":    account["name"],
            "ip":      account.get("current_ip", "—"),
            "stake":   account_stake,
            "delay":   round(delay, 2),
            "success": status == 200 and resp.get("result") == "OK",
            "receipt": resp.get("receipt"),
            "response": resp
        }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        for future in concurrent.futures.as_completed(
            {executor.submit(bet_one, acc): acc for acc in active}
        ):
            results.append(future.result())

    # Update last bet timestamp
    app.config["LAST_BET_TS"] = datetime.utcnow()

    return jsonify({"results": results, "pd": pd, "odd_check": odd_check, "blocked": False})

# ══════════════════════════════════════════════════════════════════
# ROUTES — ALL BALANCES
# ══════════════════════════════════════════════════════════════════

@app.route("/api/balances", methods=["GET"])
def get_all_balances():
    accounts = load_accounts()
    active   = [a for a in accounts if a.get("session_id") and a.get("status") == "connected"]

    def fetch_one(account):
        _, resp = qrsolver_request(
            "GET",
            f"/api/placebet/session/{account['session_id']}/balance/",
            account["api_key"]
        )
        return {
            "id":          account["id"],
            "name":        account["name"],
            "balance":     resp.get("balance"),
            "withdrawable":resp.get("withdrawable"),
            "bonus":       resp.get("bonus"),
            "currency":    resp.get("currency", ""),
            "error":       resp.get("error")
        }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        for future in concurrent.futures.as_completed(
            {executor.submit(fetch_one, acc): acc for acc in active}
        ):
            results.append(future.result())

    connected_ids = {a["id"] for a in active}
    for a in accounts:
        if a["id"] not in connected_ids:
            results.append({
                "id": a["id"], "name": a["name"],
                "balance": None, "error": "desconectada"
            })

    results.sort(key=lambda x: x["id"])
    return jsonify(results)

# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# DEBUG — test PD hash formats
# ══════════════════════════════════════════════════════════════════

@app.route("/api/debug/prematch", methods=["POST"])
def debug_prematch():
    """Try all PD formats and return raw responses for debugging."""
    import re
    data = request.json
    url  = data.get("url", "")

    accounts = load_accounts()
    account  = next((a for a in accounts if a.get("status") == "connected" and a.get("api_key")), None)
    if not account:
        return jsonify({"error": "No hay cuentas conectadas"}), 400

    api_key    = account["api_key"]
    session_id = account.get("session_id")
    proxy      = account.get("proxy", "")

    # Parse fragment from URL
    from urllib.parse import urlparse
    parsed   = urlparse(url)
    fragment = parsed.fragment  # e.g. /AC/B73/C104/D20260404/E21134093/F192388023/H0/

    # Generate all format variants
    pd_variants = {
        "hash_format":    fragment.replace("/", "#"),           # #AC#B73#...#
        "hash_no_lead":   fragment.replace("/", "#").lstrip("#"), # AC#B73#...#
        "slash_format":   fragment,                              # /AC/B73/.../
        "slash_no_lead":  fragment.lstrip("/"),                  # AC/B73/.../
        "hash_no_trail":  fragment.replace("/", "#").strip("#"), # AC#B73#...
    }

    results = {}

    # Test with existing session on guest endpoint
    for name, pd_val in pd_variants.items():
        if session_id:
            status, resp = qrsolver_request("GET",
                f"/api/placebet/guest/{session_id}/prematch",
                api_key, params={"pd": pd_val})
            raw = resp if isinstance(resp, str) else json.dumps(resp)
            results[f"existing_session_{name}"] = {
                "pd_sent": pd_val,
                "status":  status,
                "response": raw[:300]
            }

    # Also try creating a fresh guest session
    guest_body = {"domain": "https://www.bet365.com/"}
    if proxy:
        guest_body["proxy"] = proxy
    gs, gr = qrsolver_request("POST", "/api/placebet/guest/create/", api_key, guest_body)
    results["guest_session_create"] = {"status": gs, "response": json.dumps(gr)[:200]}

    if gs == 200 and "session_id" in gr:
        guest_id = gr["session_id"]
        for name, pd_val in list(pd_variants.items())[:3]:  # test 3 formats with guest
            status, resp = qrsolver_request("GET",
                f"/api/placebet/guest/{guest_id}/prematch",
                api_key, params={"pd": pd_val})
            raw = resp if isinstance(resp, str) else json.dumps(resp)
            results[f"guest_{name}"] = {
                "pd_sent":  pd_val,
                "status":   status,
                "response": raw[:400]
            }
        # Clean up guest session
        qrsolver_request("DELETE", f"/api/placebet/session/{guest_id}/", api_key)

    return jsonify({
        "fragment":  fragment,
        "account":   account["name"],
        "session_id": session_id,
        "results":   results
    })




@app.route("/api/race/refresh", methods=["POST"])
def refresh_race():
    """Refresh runner odds - same as load_runners but for updating existing race."""
    return load_runners()

# ── Race Queue (in-memory, multiple races) ────────────────────────────────
# Initialize database on startup
try:
    init_db()
    print("Database initialized OK")
except Exception as e:
    print(f"Database init error: {e}")

if "RACE_QUEUE" not in app.config:
    try:
        app.config["RACE_QUEUE"] = load_race_queue()
        print(f"Race queue loaded: {len(app.config['RACE_QUEUE'])} races")
    except Exception as e:
        print(f"Race queue load error: {e}")
        app.config["RACE_QUEUE"] = {}

import threading as _threading
RACE_QUEUE_LOCK = _threading.Lock()

def cors_response(data, status=200):
    resp = jsonify(data)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp, status

@app.route("/api/race/from-browser", methods=["POST", "OPTIONS"])
def race_from_browser():
    """Receive race data from bookmarklet. Adds/updates race in queue."""
    if request.method == "OPTIONS":
        from flask import Response as R
        r = R()
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return r

    data     = request.json
    runners  = data.get("runners", [])
    url      = data.get("url", "")
    fi        = str(data.get("fi", 0))
    sport_id  = data.get("sport_id", 73)
    race_name = data.get("race_name", "")

    if not runners:
        return cors_response({"error": "No se recibieron datos"}, 400)

    # Extract date from URL for display
    import re
    date_match = re.search(r'D(\d{8})', url)
    race_date  = date_match.group(1) if date_match else ""
    if race_date:
        race_date = race_date[6:8] + "/" + race_date[4:6] + "/" + race_date[0:4]

    # Build display name: use race_name from browser if available
    if race_name and race_name != "Carrera":
        display_name = race_name
        if race_date:
            display_name = race_name + " — " + race_date
    else:
        display_name = f"Carrera {race_date}" if race_date else f"Carrera F{fi}"

    with RACE_QUEUE_LOCK:
        queue     = app.config["RACE_QUEUE"]
        is_update = fi in queue
        queue[fi] = {
            "fi":       fi,
            "url":      url,
            "sport_id": sport_id,
            "runners":  runners,
            "date":     race_date,
            "name":     display_name,
            "ts":       datetime.utcnow().isoformat(),
            "selected": None
        }
        app.config["RACE_QUEUE"] = queue
        total = len(queue)
        save_race_queue(queue)

    action = "updated" if is_update else "added"
    return cors_response({"success": True, "count": len(runners), "fi": fi, "action": action, "total": total})


@app.route("/api/race/last", methods=["GET"])
def race_last():
    """Return most recently added race (for backward compatibility)."""
    queue = app.config.get("RACE_QUEUE", {})
    if not queue:
        return jsonify({"error": "No hay carrera cargada"}), 404
    # Return most recently added
    latest = max(queue.values(), key=lambda x: x["ts"])
    return jsonify(latest)


@app.route("/api/race/queue", methods=["GET"])
def race_queue():
    """Return all races in queue."""
    queue = app.config.get("RACE_QUEUE", {})
    races = sorted(queue.values(), key=lambda x: x["ts"], reverse=True)
    return jsonify(races)


@app.route("/api/race/remove/<fi>", methods=["DELETE"])
def race_remove(fi):
    """Remove a race from queue."""
    queue = app.config.get("RACE_QUEUE", {})
    if fi in queue:
        del queue[fi]
    return jsonify({"success": True})


@app.route("/api/race/clear", methods=["POST"])
def race_clear():
    """Clear all races from queue."""
    app.config["RACE_QUEUE"] = {}
    save_race_queue({})
    return jsonify({"success": True})

@app.route("/api/race/add-link", methods=["POST", "GET"])
def add_link():
    """Receive a single race link from iOS Shortcut."""
    url = ""

    if request.method == "GET":
        # Try different param names iOS might send
        url = request.args.get("url", "") or request.args.get("URL", "") or request.args.get("link", "")
    else:
        # POST - try JSON body and form data
        try:
            data = request.json or {}
            url  = data.get("url", "") or data.get("URL", "") or data.get("link", "")
        except:
            pass
        if not url:
            url = request.form.get("url", "") or request.form.get("URL", "")
        if not url:
            # Try raw body
            raw = request.get_data(as_text=True)
            if raw and "bet365" in raw:
                url = raw.strip()

    # Clean URL - iOS sometimes adds extra chars
    url = url.strip().strip('"').strip("'")

    if not url or "bet365" not in url:
        return jsonify({"error": "URL de Bet365 requerida", "received": url[:100] if url else "empty"}), 400

    import re
    fm = re.search(r'/F(\d+)/', url)
    fi = str(fm.group(1)) if fm else "0"

    date_match = re.search(r'D(\d{8})', url)
    race_date  = date_match.group(1) if date_match else ""
    if race_date:
        race_date = race_date[6:8] + "/" + race_date[4:6] + "/" + race_date[0:4]

    queue = app.config.get("RACE_QUEUE", {})
    if fi not in queue:
        queue[fi] = {
            "fi":       fi,
            "url":      url,
            "sport_id": 73,
            "runners":  [],
            "date":     race_date,
            "name":     f"Carrera {race_date}" if race_date else f"Carrera F{fi}",
            "ts":       datetime.utcnow().isoformat(),
            "selected": None
        }
        app.config["RACE_QUEUE"] = queue
        return jsonify({"success": True, "fi": fi, "total": len(queue), "action": "added"})
    else:
        return jsonify({"success": True, "fi": fi, "total": len(queue), "action": "already_exists"})




# ══════════════════════════════════════════════════════════════════
# SETTINGS — Global config (guest proxy, etc.)
# ══════════════════════════════════════════════════════════════════

def load_settings():
    try:
        rows = db_exec("SELECT key, value FROM settings")
        result = {"guest_proxy": "", "global_bank": 0, "max_stake1": 12, "last_distribution": ""}
        for row in rows:
            result[row["key"]] = json.loads(row["value"])
        return result
    except Exception:
        return {"guest_proxy": "", "global_bank": 0, "max_stake1": 12, "last_distribution": ""}

def save_settings(settings):
    try:
        conn = get_db()
        try:
            for key, value in settings.items():
                conn.run("""
                    INSERT INTO settings (key, value) VALUES (:key, :value)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, key=key, value=json.dumps(value))
        finally:
            conn.close()
    except Exception as e:
        print(f"Error saving settings: {e}")

def distribute_bank():
    """Distribute global bank randomly among accounts respecting max stake 1 = $12."""
    import math, random
    settings     = load_settings()
    global_bank  = float(settings.get("global_bank", 0))
    max_stake1   = float(settings.get("max_stake1", 12))
    max_bank     = max_stake1 * 100  # max bank per account

    accounts = load_accounts()
    if not accounts or global_bank <= 0:
        return {"error": "Sin cuentas o bank no configurado"}

    n = len(accounts)

    # Generate random distribution summing to global_bank
    # with max_bank per account
    remaining = global_bank
    banks = []

    for i in range(n - 1):
        accounts_left = n - i
        max_for_this  = min(max_bank, remaining - (accounts_left - 1) * 0)
        min_for_this  = 0
        if max_for_this <= 0:
            banks.append(0)
            continue
        # Random amount for this account
        amount = random.uniform(min_for_this, max_for_this)
        amount = round(amount / 100) * 100  # round to nearest 100
        amount = min(amount, max_bank)
        banks.append(amount)
        remaining -= amount

    # Last account gets the remainder
    last = max(0, min(remaining, max_bank))
    banks.append(round(last / 100) * 100)

    # Fix rounding — ensure sum equals global_bank
    diff = global_bank - sum(banks)
    banks[0] += diff

    # Save to accounts
    for i, account in enumerate(accounts):
        account["bank"] = max(0, banks[i])
        account["stake1"] = math.ceil(account["bank"] * 0.01)  # 1% rounded up

    save_accounts(accounts)

    settings["last_distribution"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    save_settings(settings)

    return {
        "success":     True,
        "distribution": [{"name": a["name"], "bank": a["bank"], "stake1": a["stake1"]} for a in accounts],
        "total":        sum(a["bank"] for a in accounts)
    }

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def update_settings():
    data     = request.json
    settings = load_settings()
    if "guest_proxy"  in data: settings["guest_proxy"]  = data["guest_proxy"].strip()
    if "global_bank"  in data: settings["global_bank"]  = float(data["global_bank"])
    if "max_stake1"   in data: settings["max_stake1"]   = float(data["max_stake1"])
    save_settings(settings)
    return jsonify({"success": True, "settings": settings})

@app.route("/api/bankroll/distribute", methods=["POST"])
def bankroll_distribute():
    """Manually trigger bank distribution."""
    result = distribute_bank()
    return jsonify(result)

@app.route("/api/bankroll/status", methods=["GET"])
def bankroll_status():
    """Get current bank distribution per account."""
    accounts = load_accounts()
    settings = load_settings()
    distribution = [{
        "name":   a["name"],
        "bank":   a.get("bank", 0),
        "stake1": a.get("stake1", 0)
    } for a in accounts]
    return jsonify({
        "global_bank":       settings.get("global_bank", 0),
        "max_stake1":        settings.get("max_stake1", 12),
        "last_distribution": settings.get("last_distribution", ""),
        "distribution":      distribution,
        "total":             sum(a.get("bank", 0) for a in accounts)
    })

@app.route("/static/extract.js")
def serve_extract_js():
    from flask import Response
    app_url = request.url_root.rstrip("/").replace("http://", "https://")
    if os.path.exists("extract.js"):
        with open("extract.js") as f:
            js = f.read()
    else:
        js = "alert('extract.js not found');"
    prefix = "window._bet365AppUrl = '" + app_url + "';\n"
    js = prefix + js
    return Response(js, mimetype="application/javascript",
                    headers={"Access-Control-Allow-Origin": "*",
                             "Cache-Control": "no-cache"})

@app.route("/manifest.json")
def manifest():
    from flask import Response
    if os.path.exists("manifest.json"):
        with open("manifest.json") as f:
            return Response(f.read(), mimetype="application/json")
    return "{}", 404

@app.route("/sw.js")
def service_worker():
    from flask import Response
    if os.path.exists("sw.js"):
        with open("sw.js") as f:
            return Response(f.read(), mimetype="application/javascript",
                          headers={"Cache-Control": "no-cache"})
    return "", 404

@app.route("/add")
def add_redirect():
    """iOS Shortcut opens this URL with the race link. Saves to queue and redirects to app."""
    import re
    from flask import redirect as _redirect

    # iOS cuts URL at # - get the full raw URL from the request
    # The full URL including fragment is in request.url
    full_url = request.url  # this is our /add?url=... URL
    
    # Extract the bet365 URL from query string
    url = request.args.get("url", "").strip()
    
    # Also check raw query string - iOS may encode differently
    if not url or "bet365" not in url:
        from urllib.parse import unquote, parse_qs
        raw_qs = request.query_string.decode("utf-8")
        print(f"DEBUG raw_qs: '{raw_qs[:200]}'")
        if "bet365" in raw_qs:
            # Extract URL after url=
            idx = raw_qs.find("url=")
            if idx >= 0:
                url = unquote(raw_qs[idx+4:])
    
    print(f"DEBUG /add received url: '{url[:100] if url else 'empty'}'")
    print(f"DEBUG full request URL: '{full_url[:200]}'")
    
    if url and "bet365" in url:
        fm = re.search(r'/F(\d+)/', url)
        fi = str(fm.group(1)) if fm else str(hash(url))[-8:]
        date_match = re.search(r'D(\d{8})', url)
        race_date  = date_match.group(1) if date_match else ""
        if race_date:
            race_date = race_date[6:8] + "/" + race_date[4:6] + "/" + race_date[0:4]
        queue = app.config.get("RACE_QUEUE", {})
        if fi not in queue:
            queue[fi] = {
                "fi": fi, "url": url, "sport_id": 73,
                "runners": [], "date": race_date,
                "name": f"Carrera {race_date}" if race_date else f"Carrera F{fi}",
                "ts": datetime.utcnow().isoformat(), "selected": None
            }
            app.config["RACE_QUEUE"] = queue
            print(f"DEBUG added race fi={fi} to queue, total={len(queue)}")
    
    return _redirect("/")

@app.route("/")
def index():
    if os.path.exists("index.html"):
        with open("index.html") as f:
            return f.read()
    return "<h1>index.html not found</h1>", 404

# ── Midnight auto-distribution ────────────────────────────────────────────────
import threading

def auto_logout_loop():
    """Auto logout all accounts if no bet placed in 30 minutes."""
    while True:
        time.sleep(60)  # Check every minute
        try:
            last_bet = app.config.get("LAST_BET_TS")
            if last_bet is None:
                continue
            minutes_idle = (datetime.utcnow() - last_bet).total_seconds() / 60
            if minutes_idle >= 30:
                accounts = load_accounts()
                active = [a for a in accounts if a.get("status") == "connected" and a.get("session_id")]
                if active:
                    print(f"Auto-logout: {len(active)} cuentas inactivas por {int(minutes_idle)} min")
                    for a in active:
                        qrsolver_request("POST", f"/api/placebet/session/{a['session_id']}/logout/", a["api_key"])
                        qrsolver_request("DELETE", f"/api/placebet/session/{a['session_id']}/", a["api_key"])
                        a["session_id"] = None
                        a["status"]     = "disconnected"
                    save_accounts(accounts)
                    app.config["LAST_BET_TS"] = None
        except Exception as e:
            print(f"Auto-logout error: {e}")

def midnight_distribution():
    while True:
        now           = datetime.utcnow()
        next_midnight = (now + __import__('datetime').timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        time.sleep((next_midnight - now).total_seconds())
        try:
            settings = load_settings()
            if settings.get("global_bank", 0) > 0:
                distribute_bank()
        except Exception:
            pass

def auto_refresh_races():
    """Auto-refresh all loaded races every day at 14:00 Spain time (UTC+1/UTC+2)."""
    import concurrent.futures
    while True:
        try:
            now_utc = datetime.utcnow()
            # Spain is UTC+1 in winter, UTC+2 in summer (DST: last Sunday March - last Sunday October)
            month = now_utc.month
            is_dst = 3 < month < 10 or (month == 3 and now_utc.day >= 25) or (month == 10 and now_utc.day < 25)
            spain_offset = 2 if is_dst else 1
            target_utc_hour = 14 - spain_offset  # 14:00 Spain = 12:00 or 13:00 UTC

            next_run = now_utc.replace(hour=target_utc_hour, minute=0, second=0, microsecond=0)
            if now_utc >= next_run:
                next_run = next_run + __import__('datetime').timedelta(days=1)

            sleep_secs = (next_run - now_utc).total_seconds()
            print(f"Auto-refresh races scheduled in {int(sleep_secs/60)} min (at 14:00 Spain)")
            time.sleep(sleep_secs)

            # Refresh all races in parallel (10 at a time)
            queue = app.config.get("RACE_QUEUE", {})
            if not queue:
                print("Auto-refresh: no races in queue")
                continue

            print(f"Auto-refresh: refreshing {len(queue)} races...")
            settings    = load_settings()
            guest_proxy = settings.get("guest_proxy", "").strip()

            def refresh_one(fi_race):
                fi, race = fi_race
                try:
                    runners, sport_id, race_fi, pd, race_name = get_runners_from_url(race["url"], guest_proxy)
                    if runners:
                        race["runners"]   = [{
                            "id": r["id"], "name": r["name"],
                            "odd": r["odd_raw"], "odd_raw": r["odd_raw"],
                            "odd_dec": r["odd_dec"], "prog_num": r.get("prog_num","")
                        } for r in runners]
                        race["ts"] = datetime.utcnow().isoformat()
                        return fi, True
                except Exception as e:
                    print(f"Auto-refresh error for {fi}: {e}")
                return fi, False

            updated = 0
            items   = list(queue.items())
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(refresh_one, items))

            for fi, ok in results:
                if ok:
                    updated += 1

            app.config["RACE_QUEUE"] = queue
            save_race_queue(queue)
            print(f"Auto-refresh done: {updated}/{len(queue)} races updated")

        except Exception as e:
            print(f"Auto-refresh error: {e}")
            time.sleep(60)

t_midnight = threading.Thread(target=midnight_distribution, daemon=True)
t_midnight.start()
t_autologout = threading.Thread(target=auto_logout_loop, daemon=True)
t_autologout.start()
t_autorefresh = threading.Thread(target=auto_refresh_races, daemon=True)
t_autorefresh.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
