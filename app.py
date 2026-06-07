import os
import hmac
import hashlib
import base64
import json
import logging
from typing import Optional
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Scheduler for delayed calls ──
scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

# ── ENV VARS ──
SHOPIFY_STORE          = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ADMIN_TOKEN    = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
RETELL_API_KEY         = os.environ.get("RETELL_API_KEY", "")
RETELL_AGENT_ID        = os.environ.get("RETELL_AGENT_ID", "")
RETELL_FROM_NUMBER     = os.environ.get("RETELL_FROM_NUMBER", "")
CALL_DELAY_MINUTES     = int(os.environ.get("CALL_DELAY_MINUTES", "30"))

# ── In-memory stores ──
active_calls: dict  = {}   # call_id       -> checkout data (call is live)
pending_calls: dict = {}   # checkout_token -> checkout data (waiting for delay timer)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    if not SHOPIFY_WEBHOOK_SECRET:
        log.warning("SHOPIFY_WEBHOOK_SECRET not set — skipping verification")
        return True
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), data, hashlib.sha256
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header or "")


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
    parts = [
        addr.get("address1", ""),
        addr.get("city", ""),
        addr.get("province", ""),
        addr.get("zip", ""),
    ]
    return ", ".join(p for p in parts if p)


def extract_checkout_data(payload: dict) -> Optional[dict]:
    try:
        checkout = payload.get("checkout", payload)
        shipping = checkout.get("shipping_address") or {}
        billing  = checkout.get("billing_address") or {}
        addr     = shipping or billing

        first_name = addr.get("first_name", "")
        last_name  = addr.get("last_name", "")
        full_name  = f"{first_name} {last_name}".strip()
        if not full_name:
            email     = checkout.get("email", "")
            full_name = email.split("@")[0] if email else "Client"

        raw_phone = checkout.get("phone") or addr.get("phone") or ""
        phone = normalize_phone(raw_phone)
        if not phone:
            log.info(f"Checkout {checkout.get('token','?')} skipped — no valid phone")
            return None

        line_items    = checkout.get("line_items", [])
        items_summary = ", ".join(
            f"{item.get('title', 'Produs')} x{item.get('quantity', 1)}"
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
            "address1":     addr.get("address1", ""),
            "city":         addr.get("city", ""),
            "province":     addr.get("province", ""),   # judetul
            "zip":          addr.get("zip", ""),
            "country_code": addr.get("country_code", "RO"),
            "phone":        phone,
        }

        has_address     = bool(address["address1"] and address["city"])
        address_preview = format_address_preview(address) if has_address else ""

        return {
            "customer_name":   full_name,
            "phone":           phone,
            "email":           checkout.get("email", ""),
            "cart_items":      items_summary,
            "cart_value":      cart_value,
            "discount_code":   "",
            "checkout_token":  checkout.get("token", ""),
            "line_items":      line_items,
            "address":         address,
            "has_address":     "da" if has_address else "nu",
            "address_preview": address_preview,
        }
    except Exception as e:
        log.error(f"extract_checkout_data error: {e}", exc_info=True)
        return None


def do_trigger_retell_call(checkout: dict):
    """
    Actually fires the Retell outbound call.
    Called by APScheduler after the delay, or directly from /test/trigger-call.
    """
    token = checkout.get("checkout_token", "")
    pending_calls.pop(token, None)

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
            active_calls[call_id] = checkout
            log.info(f"Call live: {call_id} -> {checkout['phone']} ({checkout['customer_name']})")
        return result
    except Exception as e:
        log.error(f"do_trigger_retell_call error: {e}", exc_info=True)


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":          "ok",
        "active_calls":    len(active_calls),
        "pending_calls":   len(pending_calls),
        "delay_minutes":   CALL_DELAY_MINUTES,
        "scheduler_jobs":  len(scheduler.get_jobs()),
    }), 200


@app.route("/shopify-webhook", methods=["POST"])
def shopify_webhook():
    """
    Receives Shopify checkout abandonment webhook.
    Schedules a Retell call after CALL_DELAY_MINUTES.
    If same checkout fires again (customer updated cart), resets the timer.
    """
    raw_body    = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    topic       = request.headers.get("X-Shopify-Topic", "")
    log.info(f"Webhook: topic={topic}")

    if not verify_shopify_webhook(raw_body, hmac_header):
        log.warning("Invalid Shopify HMAC — rejected")
        return jsonify({"error": "Unauthorized"}), 401

    if topic not in ("checkouts/update", "checkouts/create"):
        return jsonify({"status": "ignored"}), 200

    try:
        payload  = request.get_json(force=True)
        checkout = extract_checkout_data(payload)
        if not checkout:
            return jsonify({"status": "ignored", "reason": "missing_data"}), 200

        token  = checkout["checkout_token"]
        job_id = f"call_{token}"

        # Reset timer if checkout was updated (customer came back and changed cart)
        try:
            scheduler.remove_job(job_id)
            log.info(f"Timer reset for checkout {token}")
        except Exception:
            pass

        run_at = datetime.now() + timedelta(minutes=CALL_DELAY_MINUTES)
        scheduler.add_job(
            do_trigger_retell_call,
            trigger="date",
            run_date=run_at,
            args=[checkout],
            id=job_id,
        )
        pending_calls[token] = checkout
        log.info(f"Call scheduled: {checkout['customer_name']} @ {run_at.strftime('%H:%M:%S')} (+{CALL_DELAY_MINUTES}min)")

        return jsonify({
            "status":        "scheduled",
            "delay_minutes": CALL_DELAY_MINUTES,
            "fires_at":      run_at.isoformat(),
            "customer":      checkout["customer_name"],
            "has_address":   checkout["has_address"],
        }), 200

    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/retell-tool/update-address", methods=["POST"])
def retell_update_address():
    """
    Called by Retell agent after collecting/correcting address from customer.
    Retell body: { "call": { "call_id": "..." }, "args": { "address1": "...", "city": "...", ... } }
    Returns a preview for agent to read back to customer.
    """
    try:
        body = request.get_json(force=True) or {}
        log.info(f"update-address body: {json.dumps(body)}")

        call_info = body.get("call", {})
        call_id   = (call_info.get("call_id") or
                     request.headers.get("X-Retell-Call-Id") or "")
        args      = body.get("args", body)

        if not call_id or call_id not in active_calls:
            return jsonify({"result": "Eroare: sesiune negasita. Va rugam reincercati."}), 200

        addr = active_calls[call_id]["address"]

        # Update only fields that were provided
        field_map = {
            "address1":  "address1",
            "city":      "city",
            "county":    "province",
            "zip":       "zip",
            "first_name": "first_name",
            "last_name":  "last_name",
        }
        for arg_key, addr_key in field_map.items():
            if args.get(arg_key):
                addr[addr_key] = args[arg_key]

        preview = format_address_preview(addr)
        log.info(f"Address updated for {call_id}: {preview}")

        return jsonify({
            "result": f"Am notat adresa: {preview}. Este corecta?"
        }), 200

    except Exception as e:
        log.error(f"update-address error: {e}", exc_info=True)
        return jsonify({"result": "Eroare la actualizarea adresei."}), 200


@app.route("/retell-tool/place-order-cod", methods=["POST"])
def retell_place_order_cod():
    """
    Called by Retell agent when customer has explicitly confirmed they want the order with COD.
    Creates a direct Shopify order.
    Retell body: { "call": {...}, "args": { "confirmed": true } }
    """
    try:
        body = request.get_json(force=True) or {}
        log.info(f"place-order-cod body: {json.dumps(body)}")

        call_info = body.get("call", {})
        call_id   = (call_info.get("call_id") or request.headers.get("X-Retell-Call-Id") or "")
        args      = body.get("args", body)
        confirmed = args.get("confirmed", False)

        if not call_id:
            return jsonify({"result": "Eroare interna. Va rugam finalizati pe nixt.ro."}), 200

        if not confirmed:
            return jsonify({"result": "Comanda nu a fost confirmata."}), 200

        checkout = active_calls.get(call_id)
        if not checkout:
            return jsonify({"result": "Sesiune expirata. Va rugam finalizati pe nixt.ro."}), 200

        addr = checkout["address"]
        line_items = [
            {"variant_id": item["variant_id"], "quantity": item.get("quantity", 1)}
            for item in checkout["line_items"]
            if item.get("variant_id")
        ]

        if not line_items:
            return jsonify({"result": "Eroare la procesarea produselor. Va rugam finalizati pe nixt.ro."}), 200

        resp = requests.post(
            f"https://{SHOPIFY_STORE}/admin/api/2024-01/orders.json",
            headers={
                "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN,
                "Content-Type": "application/json",
            },
            json={
                "order": {
                    "line_items":       line_items,
                    "shipping_address": addr,
                    "billing_address":  addr,
                    "email":            checkout.get("email", ""),
                    "phone":            checkout.get("phone", ""),
                    "financial_status": "pending",   # COD — plateste la curier
                    "send_receipt":     True,
                    "note":             "Comandă plasată prin agent telefonic AI — ramburs",
                    "tags":             "ai-recovery,abandoned-checkout,ramburs",
                }
            },
            timeout=15,
        )
        resp.raise_for_status()
        
        # Clean up after successful order
        active_calls.pop(call_id, None)

        return jsonify({
            "result": "Comanda a fost plasata cu succes cu plata ramburs. Veti primi un mesaj."
        }), 200

    except requests.HTTPError as e:
        log.error(f"Shopify error: {e.response.status_code} {e.response.text}")
        return jsonify({"result": "Eroare la Shopify. Va rugam finalizati pe nixt.ro."}), 200
    except Exception as e:
        log.error(f"place-order-cod unexpected error: {e}", exc_info=True)
        return jsonify({"result": "Eroare neasteptata. Va rugam finalizati pe nixt.ro."}), 200


@app.route("/retell-tool/decline", methods=["POST"])
def retell_decline():
    body      = request.get_json(force=True) or {}
    call_info = body.get("call", {})
    call_id   = (call_info.get("call_id") or
                 request.headers.get("X-Retell-Call-Id") or "")
    if call_id:
        active_calls.pop(call_id, None)
        log.info(f"Call {call_id} declined — data cleaned up")
    return jsonify({"result": "ok"}), 200


# ─── TEST ENDPOINTS ───

@app.route("/test/trigger-call", methods=["POST"])
def test_trigger_call():
    """
    TEST ONLY — fires Retell call immediately, bypassing the delay.
    POST body (all optional — uses defaults):
    {
      "phone": "+40712345678",
      "customer_name": "Ion Test",
      "cart_items": "Gel Cuticule 60ml x1",
      "cart_value": "59",
      "has_address": "da",
      "address_preview": "Str. Test 1, Brasov, Brasov, 500001"
    }
    """
    body = request.get_json(force=True) or {}
    checkout = {
        "customer_name":   body.get("customer_name", "Ion Test"),
        "phone":           body.get("phone", RETELL_FROM_NUMBER),
        "email":           body.get("email", "test@nixt.ro"),
        "cart_items":      body.get("cart_items", "Gel Cuticule 60ml x1"),
        "cart_value":      body.get("cart_value", "59"),
        "discount_code":   body.get("discount_code", ""),
        "checkout_token":  "test_token_999",
        "has_address":     body.get("has_address", "da"),
        "address_preview": body.get("address_preview", "Str. Test 1, Brasov, Brasov, 500001"),
        "line_items":      [{"title": "Gel Cuticule 60ml", "variant_id": None, "quantity": 1}],
        "address": {
            "first_name": body.get("customer_name", "Ion").split()[0],
            "last_name":  "Test",
            "address1":   "Str. Test 1",
            "city":       "Brasov",
            "province":   "Brasov",
            "zip":        "500001",
            "country_code": "RO",
            "phone":      body.get("phone", ""),
        },
    }
    do_trigger_retell_call(checkout)
    return jsonify({"status": "triggered", "checkout": checkout}), 200


@app.route("/test/active-calls", methods=["GET"])
def test_active_calls():
    """TEST ONLY — inspect in-memory state."""
    return jsonify({
        "active_calls":  list(active_calls.keys()),
        "pending_calls": list(pending_calls.keys()),
        "scheduler_jobs": [j.id for j in scheduler.get_jobs()],
    }), 200


@app.route("/test/cancel-pending/<token>", methods=["DELETE"])
def test_cancel_pending(token: str):
    """TEST ONLY — cancel a scheduled call before it fires."""
    try:
        scheduler.remove_job(f"call_{token}")
        pending_calls.pop(token, None)
        return jsonify({"status": "cancelled", "token": token}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)