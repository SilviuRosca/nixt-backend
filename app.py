import os
import hmac
import hashlib
import base64
import json
import logging
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── ENV VARS (set in Railway/Render dashboard) ──
SHOPIFY_STORE        = os.environ.get("SHOPIFY_STORE")           # yourstore.myshopify.com
SHOPIFY_ADMIN_TOKEN  = os.environ.get("SHOPIFY_ADMIN_TOKEN")     # shpat_xxx
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET") # from Shopify webhook settings
RETELL_API_KEY       = os.environ.get("RETELL_API_KEY")          # from Retell dashboard
RETELL_AGENT_ID      = os.environ.get("RETELL_AGENT_ID")         # your agent id
RETELL_FROM_NUMBER   = os.environ.get("RETELL_FROM_NUMBER")      # your Telnyx number e.g. +40...

# In-memory store for active checkouts (use Redis in production)
# key: call_id → checkout data
active_calls = {}


# ────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────

def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    """Verify request actually came from Shopify."""
    if not SHOPIFY_WEBHOOK_SECRET:
        return True  # skip verification in dev
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        data,
        hashlib.sha256
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header or "")


def extract_checkout_data(payload: dict) -> dict | None:
    """Pull the fields we need from Shopify checkout payload."""
    try:
        checkout = payload.get("checkout", payload)  # handle both formats

        # Customer info
        billing  = checkout.get("billing_address") or {}
        shipping = checkout.get("shipping_address") or {}
        addr     = shipping or billing

        first_name = addr.get("first_name") or checkout.get("email", "").split("@")[0]
        last_name  = addr.get("last_name", "")
        full_name  = f"{first_name} {last_name}".strip() or "Client"

        phone = (
            checkout.get("phone") or
            addr.get("phone") or
            ""
        )

        # Clean phone to E.164 for Romania
        phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if phone.startswith("07") or phone.startswith("02") or phone.startswith("03"):
            phone = "+4" + phone
        if not phone.startswith("+"):
            log.warning(f"Phone number format unclear: {phone}")
            return None  # can't call without valid phone

        # Cart items
        line_items = checkout.get("line_items", [])
        items_summary = ", ".join(
            f"{item.get('title', 'Produs')} x{item.get('quantity', 1)}"
            for item in line_items[:3]  # max 3 items in summary
        )
        if len(line_items) > 3:
            items_summary += f" +{len(line_items) - 3} altele"

        total = checkout.get("total_price") or checkout.get("subtotal_price") or "0"

        # Address for order placement
        address = {
            "first_name":    addr.get("first_name", first_name),
            "last_name":     addr.get("last_name", last_name),
            "address1":      addr.get("address1", ""),
            "city":          addr.get("city", ""),
            "zip":           addr.get("zip", ""),
            "country_code":  addr.get("country_code", "RO"),
            "phone":         phone,
        }

        return {
            "customer_name":   full_name,
            "phone":           phone,
            "email":           checkout.get("email", ""),
            "cart_items":      items_summary,
            "cart_value":      str(total),
            "address":         address,
            "checkout_token":  checkout.get("token", ""),
            "line_items":      line_items,
            "discount_code":   "",  # add logic here if you use discount codes
        }

    except Exception as e:
        log.error(f"Error extracting checkout data: {e}")
        return None


def trigger_retell_call(checkout: dict) -> dict:
    """Call Retell API to start outbound call."""
    payload = {
        "from_number": RETELL_FROM_NUMBER,
        "to_number":   checkout["phone"],
        "agent_id":    RETELL_AGENT_ID,
        "retell_llm_dynamic_variables": {
            "customer_name":  checkout["customer_name"],
            "cart_items":     checkout["cart_items"],
            "cart_value":     checkout["cart_value"],
            "discount_code":  checkout.get("discount_code", ""),
        },
        # Pass checkout data as metadata so we can retrieve it when order is placed
        "metadata": {
            "checkout_token": checkout["checkout_token"],
            "email":          checkout["email"],
            "address":        checkout["address"],
            "line_items":     checkout["line_items"],
        }
    }

    resp = requests.post(
        "https://api.retellai.com/v2/create-phone-call",
        headers={
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def place_shopify_order(call_id: str) -> dict:
    """Create a real order in Shopify using stored checkout data."""
    checkout = active_calls.get(call_id)
    if not checkout:
        raise ValueError(f"No checkout data found for call_id: {call_id}")

    addr       = checkout["address"]
    line_items = checkout["line_items"]

    # Build Shopify order payload
    order_payload = {
        "order": {
            "line_items": [
                {
                    "variant_id": item.get("variant_id"),
                    "quantity":   item.get("quantity", 1),
                }
                for item in line_items
                if item.get("variant_id")
            ],
            "shipping_address": addr,
            "billing_address":  addr,
            "email":            checkout.get("email", ""),
            "phone":            checkout.get("phone", ""),
            "financial_status": "pending",
            "send_receipt":     True,
            "note":             "Order placed via AI phone agent (abandoned checkout recovery)",
            "tags":             "ai-recovery, abandoned-checkout",
        }
    }

    resp = requests.post(
        f"https://{SHOPIFY_STORE}/admin/api/2024-01/orders.json",
        headers={
            "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN,
            "Content-Type":           "application/json",
        },
        json=order_payload,
        timeout=15,
    )
    resp.raise_for_status()
    order = resp.json().get("order", {})
    log.info(f"Order created: #{order.get('order_number')} for {checkout['customer_name']}")
    return order


# ────────────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/shopify-webhook", methods=["POST"])
def shopify_webhook():
    """
    Receives abandoned checkout event from Shopify.
    Verifies signature, extracts data, triggers Retell call.
    """
    raw_body   = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    topic      = request.headers.get("X-Shopify-Topic", "")

    log.info(f"Webhook received: {topic}")

    # Verify it's really from Shopify
    if not verify_shopify_webhook(raw_body, hmac_header):
        log.warning("Invalid Shopify webhook signature")
        return jsonify({"error": "Unauthorized"}), 401

    # Only handle checkout abandonment
    if topic not in ("checkouts/update", "checkouts/create"):
        return jsonify({"status": "ignored", "topic": topic}), 200

    try:
        payload  = request.get_json(force=True)
        checkout = extract_checkout_data(payload)

        if not checkout:
            log.info("Checkout ignored: missing phone or required data")
            return jsonify({"status": "ignored", "reason": "missing_phone"}), 200

        if not checkout["phone"]:
            log.info("Checkout ignored: no phone number")
            return jsonify({"status": "ignored", "reason": "no_phone"}), 200

        # Trigger the call
        call_response = trigger_retell_call(checkout)
        call_id = call_response.get("call_id")

        # Store checkout data keyed by call_id for later order placement
        if call_id:
            active_calls[call_id] = checkout
            log.info(f"Call triggered: {call_id} → {checkout['phone']} ({checkout['customer_name']})")

        return jsonify({
            "status":  "call_triggered",
            "call_id": call_id,
            "phone":   checkout["phone"],
            "customer": checkout["customer_name"],
        }), 200

    except requests.HTTPError as e:
        log.error(f"Retell API error: {e.response.text}")
        return jsonify({"error": "retell_api_error", "detail": e.response.text}), 502
    except Exception as e:
        log.error(f"Webhook processing error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/retell-tool/order", methods=["POST"])
def retell_place_order():
    """
    Called by Retell agent tool when customer confirms they want to order.
    Retell sends call_id in headers automatically.
    """
    try:
        body    = request.get_json(force=True) or {}
        call_id = request.headers.get("X-Retell-Call-Id") or body.get("call_id", "")
        confirmed = body.get("confirmed", False)

        log.info(f"Order tool called — call_id: {call_id}, confirmed: {confirmed}")

        if not confirmed:
            return jsonify({
                "status":  "not_confirmed",
                "message": "Customer did not confirm the order.",
            }), 200

        if not call_id:
            return jsonify({"error": "Missing call_id"}), 400

        order = place_shopify_order(call_id)

        # Clean up memory
        active_calls.pop(call_id, None)

        return jsonify({
            "status":       "order_placed",
            "order_number": order.get("order_number"),
            "order_id":     order.get("id"),
            "message":      f"Order #{order.get('order_number')} placed successfully.",
        }), 200

    except ValueError as e:
        log.error(f"Order tool error: {e}")
        return jsonify({"error": str(e)}), 404
    except requests.HTTPError as e:
        log.error(f"Shopify API error: {e.response.text}")
        return jsonify({"error": "shopify_api_error", "detail": e.response.text}), 502
    except Exception as e:
        log.error(f"Order placement error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/retell-tool/decline", methods=["POST"])
def retell_decline():
    """
    Called by Retell agent when customer declines.
    Cleans up stored data.
    """
    body    = request.get_json(force=True) or {}
    call_id = request.headers.get("X-Retell-Call-Id") or body.get("call_id", "")
    if call_id:
        active_calls.pop(call_id, None)
        log.info(f"Call {call_id} declined — data cleaned up")
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
