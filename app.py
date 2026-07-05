import os
import json
import logging
from typing import Optional
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests
import redis
from retell import Retell
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

# ── ENV VARS ──
SHOPIFY_STORE           = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_CLIENT_ID       = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET   = os.environ.get("SHOPIFY_CLIENT_SECRET", "")   # this is the "Client Token" (shpss_...)
RETELL_API_KEY          = os.environ.get("RETELL_API_KEY", "")
RETELL_AGENT_ID         = os.environ.get("RETELL_AGENT_ID", "")
RETELL_FROM_NUMBER      = os.environ.get("RETELL_FROM_NUMBER", "")
COD_GATEWAY_NAME        = os.environ.get("COD_GATEWAY_NAME", "Cash on Delivery (COD)")
MAX_CALLS_PER_DAY       = int(os.environ.get("MAX_CALLS_PER_DAY", "10"))
POLL_INTERVAL_MINUTES   = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
MIN_ABANDON_AGE_MINUTES = int(os.environ.get("MIN_ABANDON_AGE_MINUTES", "30"))
REDIS_URL               = os.environ.get("REDIS_URL", "redis://localhost:6379")
CALLED_TOKEN_TTL_DAYS   = int(os.environ.get("CALLED_TOKEN_TTL_DAYS", "90"))   # matches Shopify's own 3-month abandoned-checkout retention
ACTIVE_CALL_TTL_SECONDS = int(os.environ.get("ACTIVE_CALL_TTL_SECONDS", "3600"))  # 1h safety net so stale calls don't linger forever

retell_client = Retell(api_key=RETELL_API_KEY)


# ─────────────────────────────────────────────
# REDIS — shared state across workers/instances
# ─────────────────────────────────────────────

class _FakeRedis:
    """
    Minimal in-memory Redis stand-in for LOCAL TESTING ONLY when no Redis is
    reachable. Has NONE of Redis's cross-process guarantees — do not rely on
    this for anything beyond running app.py directly on your own machine.
    """
    def __init__(self):
        self._store: dict = {}
        self._expiry: dict = {}

    def _expired(self, key):
        exp = self._expiry.get(key)
        return exp is not None and datetime.now(timezone.utc).timestamp() > exp

    def _purge(self, key):
        if self._expired(key):
            self._store.pop(key, None)
            self._expiry.pop(key, None)

    def ping(self):
        return True

    def get(self, key):
        self._purge(key)
        return self._store.get(key)

    def set(self, key, value, nx=False, ex=None):
        self._purge(key)
        if nx and key in self._store:
            return None
        self._store[key] = value
        if ex:
            self._expiry[key] = datetime.now(timezone.utc).timestamp() + ex
        return True

    def incr(self, key, amount=1):
        self._purge(key)
        val = int(self._store.get(key, 0)) + amount
        self._store[key] = str(val)
        return val

    def expire(self, key, seconds):
        self._expiry[key] = datetime.now(timezone.utc).timestamp() + seconds
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                n += 1
            self._expiry.pop(k, None)
        return n

    def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in list(self._store.keys()) if k.startswith(prefix) and not self._expired(k)]

    def ttl(self, key):
        self._purge(key)
        if key not in self._store:
            return -2
        exp = self._expiry.get(key)
        if exp is None:
            return -1
        return max(int(exp - datetime.now(timezone.utc).timestamp()), 0)


try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    log.info("Connected to Redis")
except Exception as e:
    log.warning(f"Redis unavailable ({e}) — using in-memory fallback. "
                f"NOT SAFE for multi-worker/multi-instance production use!")
    redis_client = _FakeRedis()


# ─── Active calls (call_id -> checkout data), used by Retell tool callbacks ───
# Redis-backed so any worker/instance handling the callback can read it,
# not just the one that happened to trigger the outbound call.

def save_active_call(call_id: str, checkout: dict):
    redis_client.set(f"call:{call_id}", json.dumps(checkout), ex=ACTIVE_CALL_TTL_SECONDS)


def get_active_call(call_id: str) -> Optional[dict]:
    raw = redis_client.get(f"call:{call_id}")
    return json.loads(raw) if raw else None


def delete_active_call(call_id: str):
    redis_client.delete(f"call:{call_id}")


# ─── Dedup claim (checkout_token), atomic across workers/instances ───

def try_claim_checkout(token: str) -> bool:
    """
    Atomically marks a checkout token as claimed. Returns True if THIS call
    won the claim and should proceed; False if it was already claimed
    (by this or another process) and should be skipped.
    """
    key = f"called:{token}"
    ttl_seconds = CALLED_TOKEN_TTL_DAYS * 24 * 3600
    return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds))


def release_claim(token: str):
    """Called when triggering the call failed, so it can be retried next poll cycle."""
    redis_client.delete(f"called:{token}")


# ─── Daily call counter — UTC-based everywhere to avoid local/UTC drift ───

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def calls_made_today() -> int:
    val = redis_client.get(f"calls:{_today_key()}")
    return int(val) if val else 0


def increment_calls_today() -> int:
    key = f"calls:{_today_key()}"
    new_val = redis_client.incr(key)
    if new_val == 1:
        redis_client.expire(key, 3 * 24 * 3600)  # safety TTL, well past a single day
    return new_val


# ─────────────────────────────────────────────
# RETELL SIGNATURE VERIFICATION
# ─────────────────────────────────────────────

def verify_retell_signature(raw_body: bytes, signature: str) -> bool:
    """
    Confirms the request actually came from Retell, per:
    https://docs.retellai.com/build/conversation-flow/custom-function
    Uses RETELL_API_KEY as the verification secret — the same key already
    used to create outbound calls.
    """
    if not signature:
        return False
    try:
        return retell_client.verify(
            raw_body.decode("utf-8"),
            api_key=RETELL_API_KEY,
            signature=signature,
        )
    except Exception as e:
        log.error(f"Signature verification error: {e}")
        return False


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def normalize_phone(phone: str) -> Optional[str]:
    if not phone:
        return None
    p = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if p.startswith("+40"):                       return p
    if p.startswith("07") or p.startswith("02") or p.startswith("03"): return "+4" + p
    if p.startswith("40") and len(p) >= 11:       return "+" + p
    if p.startswith("+") and len(p) >= 10:        return p
    log.warning(f"Could not normalize phone: {phone!r}")
    return None


def format_address_preview(addr: dict) -> str:
    parts = [addr.get("address1", ""), addr.get("city", ""), addr.get("province", ""), addr.get("zip", "")]
    return ", ".join(p for p in parts if p)


def is_old_enough(created_at_str: str) -> bool:
    """Extra age filter on top of Shopify's own ~10min abandonment threshold. UTC-based."""
    if not created_at_str:
        return True
    try:
        created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60
        return age_minutes >= MIN_ABANDON_AGE_MINUTES
    except Exception:
        return True


def extract_checkout_data(checkout: dict) -> Optional[dict]:
    """`checkout` is one item from Shopify's Abandoned Checkouts REST list."""
    try:
        shipping = checkout.get("shipping_address") or {}
        billing  = checkout.get("billing_address") or {}
        addr     = shipping or billing

        # IMPORTANT: use `.get(key) or default`, NOT `.get(key, default)`.
        # Shopify often sends explicit JSON nulls (e.g. "first_name": null)
        # rather than omitting the key — `.get(key, default)` only falls
        # back to default when the KEY IS MISSING, not when its value is
        # None. That previously let literal "None" leak into customer_name
        # and get spoken aloud by the agent on real calls.
        first_name = addr.get("first_name") or ""
        last_name  = addr.get("last_name") or ""
        full_name  = f"{first_name} {last_name}".strip()
        if not full_name:
            email     = checkout.get("email") or ""
            full_name = email.split("@")[0] if email else "Client"

        raw_phone = checkout.get("phone") or addr.get("phone") or ""
        phone = normalize_phone(raw_phone)
        if not phone:
            return None

        line_items    = checkout.get("line_items") or []
        items_summary = ", ".join(
            f"{(item.get('title') or 'Produs')} x{(item.get('quantity') or 1)}"
            for item in line_items[:3]
        )
        if len(line_items) > 3:
            items_summary += f" +{len(line_items) - 3} altele"

        raw_total = str(checkout.get("total_price") or checkout.get("subtotal_price") or "0")
        try:
            cart_value = str(int(float(raw_total)))
        except Exception:
            cart_value = raw_total

        address = {
            "first_name":   first_name,
            "last_name":    last_name,
            "address1":     addr.get("address1") or "",
            "city":         addr.get("city") or "",
            "province":     addr.get("province") or "",
            "zip":          addr.get("zip") or "",
            "country_code": addr.get("country_code") or "RO",
            "phone":        phone,
        }
        has_address     = bool(address["address1"] and address["city"])
        address_preview = format_address_preview(address) if has_address else ""

        return {
            "customer_name":   full_name,
            "phone":           phone,
            "email":           checkout.get("email") or "",
            "cart_items":      items_summary,
            "cart_value":      cart_value,
            "discount_code":   "",
            "checkout_token":  checkout.get("token") or "",
            "line_items":      line_items,
            "address":         address,
            "has_address":     "da" if has_address else "nu",
            "address_preview": address_preview,
        }
    except Exception as e:
        log.error(f"extract_checkout_data error: {e}", exc_info=True)
        return None


# ─────────────────────────────────────────────
# SHOPIFY — OAUTH CLIENT CREDENTIALS GRANT
# ─────────────────────────────────────────────
# Since Jan 1 2026, Shopify no longer issues permanent shpat_ tokens for new
# custom apps. Instead, we exchange SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
# for a short-lived (24h) access token via the OAuth client_credentials grant.
# https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant

def get_shopify_access_token() -> str:
    """
    Returns a valid Shopify Admin API access token, cached in Redis.
    Refreshes automatically ~5 minutes before the real 24h expiry.
    """
    cached = redis_client.get("shopify_access_token")
    if cached:
        return cached

    resp = requests.post(
        f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "grant_type":    "client_credentials",
            "client_id":     SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data       = resp.json()
    token      = data["access_token"]
    expires_in = data.get("expires_in", 86399)

    ttl = max(expires_in - 300, 60)  # refresh 5 min early, never cache for <60s
    redis_client.set("shopify_access_token", token, ex=ttl)
    log.info(f"Fetched new Shopify access token, expires in {expires_in}s")
    return token


def shopify_api_request(method: str, path: str, **kwargs) -> requests.Response:
    """
    Wraps a Shopify Admin API call using the cached OAuth token.
    If Shopify rejects the cached token (401), clears the cache and retries
    once with a freshly fetched token.
    """
    url     = f"https://{SHOPIFY_STORE}{path}"
    headers = kwargs.pop("headers", {})
    headers["X-Shopify-Access-Token"] = get_shopify_access_token()

    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 401:
        log.warning("Shopify token rejected mid-flight — refreshing and retrying once")
        redis_client.delete("shopify_access_token")
        headers["X-Shopify-Access-Token"] = get_shopify_access_token()
        resp = requests.request(method, url, headers=headers, **kwargs)
    return resp


# ─────────────────────────────────────────────
# SHOPIFY — ABANDONED CHECKOUTS POLLING
# ─────────────────────────────────────────────

def fetch_abandoned_checkouts() -> list:
    try:
        resp = shopify_api_request(
            "GET", "/admin/api/2024-01/checkouts.json",
            params={"status": "abandoned", "limit": 250},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("checkouts", [])
    except requests.HTTPError as e:
        log.error(f"fetch_abandoned_checkouts HTTP error: {e.response.status_code} {e.response.text}")
        return []
    except Exception as e:
        log.error(f"fetch_abandoned_checkouts error: {e}", exc_info=True)
        return []


def _parse_created_at(ts: str) -> datetime:
    """Parses Shopify's ISO8601 created_at for sorting. Unparseable -> oldest possible."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def process_abandoned_checkouts():
    """
    Runs every POLL_INTERVAL_MINUTES.
    MAX_CALLS_PER_DAY gates NEW call initiation only — it never blocks
    finishing a call/order already in progress (see place_cod_order).
    """
    if calls_made_today() >= MAX_CALLS_PER_DAY:
        log.info(f"Daily call limit reached ({MAX_CALLS_PER_DAY}) — skipping poll cycle entirely")
        return

    raw_checkouts = fetch_abandoned_checkouts()
    log.info(f"Poll cycle: {len(raw_checkouts)} abandoned checkouts from Shopify")

    # Shopify's checkouts.json has no `order`/`sort` param (only since_id,
    # which paginates ascending) — the default is oldest-first. Sort here
    # so we call the NEWEST eligible abandoned checkouts first instead.
    raw_checkouts.sort(key=lambda c: _parse_created_at(c.get("created_at", "")), reverse=True)

    for raw in raw_checkouts:
        token = raw.get("token", "")
        if not token:
            continue
        if not is_old_enough(raw.get("created_at", "")):
            continue
        if calls_made_today() >= MAX_CALLS_PER_DAY:
            log.info("Daily call limit reached mid-cycle — stopping further calls this cycle")
            break

        # Atomic claim — safe even if multiple workers/instances poll concurrently
        if not try_claim_checkout(token):
            continue

        checkout = extract_checkout_data(raw)
        if not checkout:
            continue  # no usable phone — claim stays, don't retry this one forever

        result = do_trigger_retell_call(checkout)
        if not result or not result.get("call_id"):
            # Call failed to initiate — release the claim so it's retried next cycle
            release_claim(token)


def do_trigger_retell_call(checkout: dict) -> Optional[dict]:
    """Fires the Retell outbound call. Only touches shared state on success."""
    payload = {
        "from_number": RETELL_FROM_NUMBER,
        "to_number":   checkout["phone"],
        "agent_id":    RETELL_AGENT_ID,
        "retell_llm_dynamic_variables": {
            "customer_name":   checkout["customer_name"],
            "cart_items":      checkout["cart_items"],
            "cart_value":      checkout["cart_value"],
            "discount_code":   checkout.get("discount_code", ""),
            "has_address":     checkout.get("has_address", "nu"),
            "address_preview": checkout.get("address_preview", ""),
        },
    }
    try:
        resp = requests.post(
            "https://api.retellai.com/v2/create-phone-call",
            headers={"Authorization": f"Bearer {RETELL_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        result  = resp.json()
        call_id = result.get("call_id")
        if call_id:
            save_active_call(call_id, checkout)
            increment_calls_today()
            log.info(f"Call live: {call_id} -> {checkout['phone']} ({checkout['customer_name']}) "
                     f"({calls_made_today()}/{MAX_CALLS_PER_DAY} today)")
        return result
    except Exception as e:
        log.error(f"do_trigger_retell_call error: {e}", exc_info=True)
        return None


def place_cod_order(call_id: str) -> dict:
    """
    Creates a REAL Shopify order with Cash on Delivery.
    NOTE: intentionally does NOT check MAX_CALLS_PER_DAY — that cap only
    gates whether a NEW call gets initiated. A call already in progress,
    where the customer already said yes, must always be able to finish.
    """
    checkout = get_active_call(call_id)
    if not checkout:
        raise ValueError(f"No checkout data for call_id: {call_id}")

    addr = checkout["address"]
    line_items = [
        {"variant_id": item["variant_id"], "quantity": item.get("quantity", 1)}
        for item in checkout["line_items"]
        if item.get("variant_id")
    ]
    if not line_items:
        raise ValueError("No line items with variant_id — cannot create order")

    resp = shopify_api_request(
        "POST", "/admin/api/2024-01/orders.json",
        headers={"Content-Type": "application/json"},
        json={
            "order": {
                "line_items":       line_items,
                "shipping_address": addr,
                "billing_address":  addr,
                "email":            checkout.get("email", ""),
                "phone":            checkout.get("phone", ""),
                "financial_status": "pending",
                "send_receipt":     True,
                "note":             "Comandă plasată prin agent telefonic AI — plată ramburs",
                "tags":             "ai-recovery,abandoned-checkout,ramburs",
                "transactions": [
                    {"kind": "sale", "status": "pending", "gateway": COD_GATEWAY_NAME}
                ],
            }
        },
        timeout=15,
    )
    resp.raise_for_status()
    order = resp.json().get("order", {})
    log.info(f"COD order #{order.get('order_number')} created for {checkout['customer_name']}")
    return order


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    try:
        redis_client.ping()
        redis_status = "connected"
    except Exception as e:
        redis_status = f"error: {e}"

    try:
        shopify_token_ttl = redis_client.ttl("shopify_access_token")
    except Exception:
        shopify_token_ttl = None

    return jsonify({
        "status":                  "ok",
        "redis":                   redis_status,
        "shopify_token_ttl_seconds": shopify_token_ttl,
        "calls_today":             calls_made_today(),
        "max_calls_per_day":       MAX_CALLS_PER_DAY,
        "poll_interval_minutes":   POLL_INTERVAL_MINUTES,
        "min_abandon_age_minutes": MIN_ABANDON_AGE_MINUTES,
        "scheduler_jobs":          len(scheduler.get_jobs()),
    }), 200


@app.route("/retell-tool/update-address", methods=["POST"])
def retell_update_address():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Retell-Signature", "")
    if not verify_retell_signature(raw_body, signature):
        log.warning("Invalid Retell signature on update-address")
        return jsonify({"result": "Unauthorized"}), 401

    try:
        body = json.loads(raw_body)
        log.info(f"update-address body: {json.dumps(body)}")

        call_info = body.get("call", {})
        call_id   = call_info.get("call_id") or ""
        args      = body.get("args", body)

        checkout = get_active_call(call_id)
        if not checkout:
            return jsonify({"result": "Eroare: sesiune negasita. Va rugam reincercati."}), 200

        addr = checkout["address"]
        field_map = {"address1": "address1", "city": "city", "county": "province",
                     "zip": "zip", "first_name": "first_name", "last_name": "last_name"}
        for arg_key, addr_key in field_map.items():
            if args.get(arg_key):
                addr[addr_key] = args[arg_key]

        save_active_call(call_id, checkout)  # persist the update, refresh TTL

        preview = format_address_preview(addr)
        log.info(f"Address updated for {call_id}: {preview}")
        return jsonify({"result": f"Am notat adresa: {preview}. Este corecta?"}), 200

    except Exception as e:
        log.error(f"update-address error: {e}", exc_info=True)
        return jsonify({"result": "Eroare la actualizarea adresei."}), 200


@app.route("/retell-tool/place-order-cod", methods=["POST"])
def retell_place_order_cod():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Retell-Signature", "")
    if not verify_retell_signature(raw_body, signature):
        log.warning("Invalid Retell signature on place-order-cod")
        return jsonify({"result": "Unauthorized"}), 401

    try:
        body = json.loads(raw_body)
        log.info(f"place-order-cod body: {json.dumps(body)}")

        call_info = body.get("call", {})
        call_id   = call_info.get("call_id") or ""
        args      = body.get("args", body)
        confirmed = args.get("confirmed", False)

        if not call_id:
            return jsonify({"result": "Eroare interna. Va rugam finalizati pe nixt.ro."}), 200
        if not confirmed:
            return jsonify({"result": "Va rog confirmati mai intai comanda."}), 200

        checkout   = get_active_call(call_id) or {}
        cart_value = checkout.get("cart_value", "")
        order      = place_cod_order(call_id)
        delete_active_call(call_id)

        return jsonify({
            "result": (
                f"Comanda a fost plasata cu succes, numarul {order.get('order_number')}. "
                f"Veti plati {cart_value} lei ramburs, la livrare. "
                f"Un curier va va contacta in curand."
            )
        }), 200

    except ValueError as e:
        log.error(f"place-order-cod ValueError: {e}")
        return jsonify({"result": "Nu am putut plasa comanda. Va rugam finalizati pe nixt.ro."}), 200
    except requests.HTTPError as e:
        log.error(f"Shopify error: {e.response.status_code} {e.response.text}")
        return jsonify({"result": "Eroare la Shopify. Va rugam finalizati pe nixt.ro."}), 200
    except Exception as e:
        log.error(f"place-order-cod unexpected error: {e}", exc_info=True)
        return jsonify({"result": "Eroare neasteptata. Va rugam finalizati pe nixt.ro."}), 200


@app.route("/retell-tool/decline", methods=["POST"])
def retell_decline():
    raw_body  = request.get_data()
    signature = request.headers.get("X-Retell-Signature", "")
    if not verify_retell_signature(raw_body, signature):
        log.warning("Invalid Retell signature on decline")
        return jsonify({"result": "Unauthorized"}), 401

    body      = json.loads(raw_body)
    call_info = body.get("call", {})
    call_id   = call_info.get("call_id") or ""
    if call_id:
        delete_active_call(call_id)
        log.info(f"Call {call_id} declined — data cleaned up")
    return jsonify({"result": "ok"}), 200


# ─── TEST ENDPOINTS ───

@app.route("/test/poll-now", methods=["POST"])
def test_poll_now():
    process_abandoned_checkouts()
    return jsonify({"status": "polled", "calls_today": calls_made_today()}), 200


@app.route("/test/trigger-call", methods=["POST"])
def test_trigger_call():
    """TEST ONLY — fires a Retell call immediately with synthetic data, bypassing Shopify entirely."""
    body = request.get_json(force=True) or {}
    checkout = {
        "customer_name":   body.get("customer_name", "Ion Test"),
        "phone":           body.get("phone", RETELL_FROM_NUMBER),
        "email":           body.get("email", "test@nixt.ro"),
        "cart_items":      body.get("cart_items", "Gel Cuticule 60ml x1"),
        "cart_value":      body.get("cart_value", "59"),
        "discount_code":   body.get("discount_code", ""),
        "checkout_token":  body.get("checkout_token", "test_token_999"),
        "has_address":     body.get("has_address", "da"),
        "address_preview": body.get("address_preview", "Str. Test 1, Brasov, Brasov, 500001"),
        "line_items":      [{"title": "Gel Cuticule 60ml", "variant_id": None, "quantity": 1}],
        "address": {
            "first_name": body.get("customer_name", "Ion").split()[0],
            "last_name":  "Test", "address1": "Str. Test 1", "city": "Brasov",
            "province": "Brasov", "zip": "500001", "country_code": "RO",
            "phone": body.get("phone", ""),
        },
    }
    result = do_trigger_retell_call(checkout)
    return jsonify({"status": "triggered" if result else "failed", "checkout": checkout}), 200


@app.route("/test/active-calls", methods=["GET"])
def test_active_calls():
    call_keys   = redis_client.keys("call:*")
    called_keys = redis_client.keys("called:*")
    return jsonify({
        "active_calls":       [k.split(":", 1)[1] for k in call_keys],
        "called_tokens_count": len(called_keys),
        "calls_today":        calls_made_today(),
    }), 200


@app.route("/test/reset-call-count", methods=["POST"])
def test_reset_call_count():
    redis_client.delete(f"calls:{_today_key()}")
    return jsonify({"status": "reset", "calls_today": 0}), 200


@app.route("/test/reset-called-tokens", methods=["POST"])
def test_reset_called_tokens():
    keys = redis_client.keys("called:*")
    if keys:
        redis_client.delete(*keys)
    return jsonify({"status": "reset", "cleared": len(keys)}), 200


# ─────────────────────────────────────────────
# STARTUP — schedule the recurring poll job
# ─────────────────────────────────────────────

scheduler.add_job(
    process_abandoned_checkouts,
    trigger="interval",
    minutes=POLL_INTERVAL_MINUTES,
    id="poll_abandoned_checkouts",
    next_run_time=datetime.now(),
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)