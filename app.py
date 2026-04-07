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

DATA_FILE    = "accounts.json"
IP_HIST_FILE = "ip_history.json"
QRSOLVER_BASE = "https://qrsolver.com"
MAX_IP_RETRIES = 15   # Max reconnection attempts to get a fresh unique IP

# ══════════════════════════════════════════════════════════════════
# STORAGE
# ══════════════════════════════════════════════════════════════════

def load_accounts():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []

def save_accounts(accounts):
    with open(DATA_FILE, "w") as f:
        json.dump(accounts, f, indent=2)

# ── IP History Registry ────────────────────────────────────────────────────────
# {
#   "190.12.34.56": {
#       "account_id": 2,
#       "account_name": "Cuenta 2",
#       "first_seen": "2025-01-01T10:00:00",
#       "last_seen":  "2025-01-01T10:00:00",
#       "times_used": 3
#   },
#   ...
# }

def load_ip_history():
    if os.path.exists(IP_HIST_FILE):
        with open(IP_HIST_FILE) as f:
            return json.load(f)
    return {}

def save_ip_history(history):
    with open(IP_HIST_FILE, "w") as f:
        json.dump(history, f, indent=2)

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
    return {"Authorization": api_key, "Content-Type": "application/json"}

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
        "api_key":      data["api_key"],
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
            a["session_id"] = result.get("session_id")
            a["status"]     = "connected" if result["success"] else "error"
            a["current_ip"] = result.get("ip")
            a["ip_log"]     = result.get("ip_log", [])
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
    qrsolver_request("POST", f"/api/placebet/session/{account['session_id']}/logout/", account["api_key"])
    for a in accounts:
        if a["id"] == account_id:
            a["session_id"] = None
            a["status"]     = "disconnected"
            # NOTE: current_ip is kept — history is permanent
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
    Parse bet365 raw data stream to extract horse runners.
    Bet365 uses a pipe-delimited proprietary format.
    Handles both JSON responses and raw text streams.
    """
    import re
    runners = []
    if not raw:
        return runners

    text = raw if isinstance(raw, str) else json.dumps(raw)

    seen_names = set()
    seen_ids   = set()

    # Pattern 1: ID|Name|Odd (pipe delimited — bet365 stream format)
    # Example: 192388023|El Vikingo|9/2
    p1 = re.compile(r'(\d{6,12})\|([A-Za-z][A-Za-z0-9 \'\-\.áéíóúñÁÉÍÓÚÑ]{2,35})\|(\d+/\d+|\d+\.\d{1,2})')
    for m in p1.finditer(text):
        sel_id = int(m.group(1))
        name   = m.group(2).strip()
        odd    = m.group(3).strip()
        dec    = odd_to_decimal(odd)
        if dec and dec > 1.0 and name not in seen_names and sel_id not in seen_ids:
            seen_names.add(name)
            seen_ids.add(sel_id)
            runners.append({"id": sel_id, "name": name, "odd_raw": odd, "odd_dec": dec})

    # Pattern 2: semicolon delimited
    if not runners:
        p2 = re.compile(r'(\d{6,12});([A-Za-z][A-Za-z0-9 \'\-\.áéíóúñ]{2,35});(\d+/\d+|\d+\.\d{1,2})')
        for m in p2.finditer(text):
            sel_id = int(m.group(1))
            name   = m.group(2).strip()
            odd    = m.group(3).strip()
            dec    = odd_to_decimal(odd)
            if dec and dec > 1.0 and name not in seen_names and sel_id not in seen_ids:
                seen_names.add(name)
                seen_ids.add(sel_id)
                runners.append({"id": sel_id, "name": name, "odd_raw": odd, "odd_dec": dec})

    # Pattern 3: any separator — broad fallback
    if not runners:
        p3 = re.compile(r'(\d{6,12})[^\d]([A-Za-z][A-Za-z0-9 \'\-\.]{2,35})[^\d](\d+/\d+)')
        for m in p3.finditer(text):
            sel_id = int(m.group(1))
            name   = m.group(2).strip()
            odd    = m.group(3).strip()
            dec    = odd_to_decimal(odd)
            if dec and dec > 1.0 and name not in seen_names and sel_id not in seen_ids:
                seen_names.add(name)
                seen_ids.add(sel_id)
                runners.append({"id": sel_id, "name": name, "odd_raw": odd, "odd_dec": dec})

    return sorted(runners, key=lambda x: x["odd_dec"])


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
    if pd.endswith("#"):
        pd = pd[:-1]

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

    # Logout existing session if any (free the slot)
    if session_id:
        qrsolver_request("POST", f"/api/placebet/session/{session_id}/logout/", api_key)

    # Create guest session (slot should be free now)
    try:
        guest_body = {"domain": domain}
        if proxy:
            guest_body["proxy"] = proxy

        gs, gr = qrsolver_request("POST", "/api/placebet/guest/create/", api_key, guest_body)

        if gs == 200 and "session_id" in gr:
            guest_id = gr["session_id"]

            # Get prematch data
            _, pr = qrsolver_request(
                "GET",
                f"/api/placebet/guest/{guest_id}/prematch",
                api_key,
                params={"pd": pd}
            )
            raw = pr if isinstance(pr, str) else json.dumps(pr)

            if "invalid" not in raw.lower() and "error" not in raw.lower():
                runners = parse_prematch_runners(raw)

            # Close guest session immediately
            qrsolver_request("DELETE", f"/api/placebet/session/{guest_id}/", api_key)
        else:
            fetch_error = f"Error creando guest: {gr}"

    except Exception as e:
        fetch_error = str(e)

    # Step 3: Re-login account session
    try:
        login_body = {
            "domain":       domain,
            "username":     account["username"],
            "password":     account["password"],
            "country_code": account["country_code"],
            "keepalive":    True
        }
        if proxy:
            login_body["proxy"] = proxy

        # Create new session
        cs, cr = qrsolver_request("POST", "/api/placebet/create/", api_key, login_body)
        if cs == 200 and "session_id" in cr:
            new_session_id = cr["session_id"]
            # Login
            qrsolver_request("POST", f"/api/placebet/session/{new_session_id}/login/", api_key, login_body)
            # Update account
            fresh = load_accounts()
            for a in fresh:
                if a["id"] == account["id"]:
                    a["session_id"] = new_session_id
                    a["status"]     = "connected"
            save_accounts(fresh)

    except Exception as e:
        pass  # Re-login failed but we still return runners

    return jsonify({
        "runners":     runners,
        "pd":          pd,
        "sport_id":    sport_id,
        "fi":          fi,
        "fetch_error": fetch_error,
        "raw_sample":  raw[:600] if raw else ""
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
    min_odd      = data.get("min_odd")        # minimum decimal odd user set
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

    # ── Odd protection: re-check current odd before firing ─────────────
    original_decimal = odd_to_decimal(odd_raw)
    odd_check = {"checked": False, "blocked": False, "reason": ""}

    if original_decimal:
        api_key    = active[0]["api_key"]
        proxy      = active[0].get("proxy", "")
        guest_body = {"domain": "https://www.bet365.com/"}
        if proxy:
            guest_body["proxy"] = proxy
        gs, gr  = qrsolver_request("POST", "/api/placebet/guest/create/", api_key, guest_body)
        if gs == 200 and "session_id" in gr:
            _, pr   = qrsolver_request("GET",
                                       f"/api/placebet/guest/{gr['session_id']}/prematch",
                                       api_key, params={"pd": pd})
            runners = parse_prematch_runners(pr if isinstance(pr, str) else json.dumps(pr))
            current = next((r for r in runners if r["id"] == int(selection_id)), None)

            if current:
                odd_check["checked"]      = True
                odd_check["current_odd"]  = current["odd_dec"]
                odd_check["original_odd"] = original_decimal
                drop = (original_decimal - current["odd_dec"]) / original_decimal * 100
                odd_check["drop_pct"]     = round(drop, 1)

                if min_odd and current["odd_dec"] < float(min_odd):
                    odd_check["blocked"] = True
                    odd_check["reason"]  = f"Cuota {current['odd_dec']} por debajo del mínimo {min_odd}"

                elif drop > odd_drop_pct:
                    odd_check["blocked"] = True
                    odd_check["reason"]  = f"Cuota cayó {drop:.1f}% — supera el límite del {odd_drop_pct}%"

                if not odd_check["blocked"]:
                    odd_raw = current["odd_raw"]   # use fresh odd

    if odd_check.get("blocked"):
        return jsonify({"blocked": True, "odd_check": odd_check, "results": []})

    # ── Fire on all accounts in parallel ───────────────────────────────
    def bet_one(account):
        bet_body = {
            "type": "singles",
            "selections": [{
                "sport_id": sport_id,
                "fi":       fi,
                "id":       selection_id,
                "odd":      odd_raw,
                "stake":    stake
            }],
            "stake": stake
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
            "success": status == 200 and resp.get("result") == "OK",
            "receipt": resp.get("receipt"),
            "response": resp
        }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for future in concurrent.futures.as_completed(
            {executor.submit(bet_one, acc): acc for acc in active}
        ):
            results.append(future.result())

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
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
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

@app.route("/api/race/from-browser", methods=["POST"])
def race_from_browser():
    """Receive race data extracted by the bookmarklet from the user's browser."""
    data    = request.json
    runners = data.get("runners", [])
    url     = data.get("url", "")

    if not runners:
        return jsonify({"error": "No se recibieron datos"}), 400

    # Store temporarily in memory (overwrite each time)
    app.config["LAST_RACE"] = {
        "runners": runners,
        "url":     url,
        "ts":      datetime.utcnow().isoformat()
    }
    return jsonify({"success": True, "count": len(runners)})


@app.route("/api/race/last", methods=["GET"])
def race_last():
    """Return last race data received from the bookmarklet."""
    race = app.config.get("LAST_RACE")
    if not race:
        return jsonify({"error": "No hay carrera cargada"}), 404
    return jsonify(race)



@app.route("/static/extract.js")
def serve_extract_js():
    from flask import Response
    app_url = request.url_root.rstrip("/")
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

@app.route("/")
def index():
    if os.path.exists("index.html"):
        with open("index.html") as f:
            return f.read()
    return "<h1>index.html not found</h1>", 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
