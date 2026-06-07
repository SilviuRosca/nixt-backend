# Nixt.ro — Retell AI Abandoned Checkout Backend

Flask backend that connects Shopify abandoned checkouts → Retell AI phone agent → Shopify order placement.

## Flow

```
Shopify abandons checkout
        ↓
POST /shopify-webhook  ← Shopify fires this
        ↓
Backend extracts: name, phone, address, cart items
        ↓
POST Retell API → outbound call starts
        ↓
Agent talks to customer
        ↓
Customer says YES → Retell calls POST /retell-tool/order
        ↓
Backend creates real order in Shopify
```

---

## Deploy to Railway (5 minutes)

1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variables (see below)
5. Railway gives you a public URL like `https://nixt-backend.up.railway.app`

### Environment Variables

Set these in Railway Dashboard → Variables:

| Variable | Where to find it |
|---|---|
| `SHOPIFY_STORE` | yourstore.myshopify.com |
| `SHOPIFY_ADMIN_TOKEN` | Shopify Admin → Settings → Apps → Develop apps → your app → Admin API token |
| `SHOPIFY_WEBHOOK_SECRET` | Shopify Admin → Settings → Notifications → Webhooks → (shown after creation) |
| `RETELL_API_KEY` | Retell Dashboard → API Keys |
| `RETELL_AGENT_ID` | Retell Dashboard → your agent → copy ID |
| `RETELL_FROM_NUMBER` | Your Telnyx number in E.164 format: +40xxxxxxxxx |

---

## Shopify Setup

### 1. Create Admin API app
Shopify Admin → Settings → Apps and sales channels → Develop apps → Create app

Permissions needed:
- `read_orders` + `write_orders`
- `read_checkouts`

### 2. Add Webhook
Shopify Admin → Settings → Notifications → Webhooks → Add webhook:
- **Event:** `Checkout update`
- **URL:** `https://your-railway-url.up.railway.app/shopify-webhook`
- **Format:** JSON

Copy the **Signing secret** → paste as `SHOPIFY_WEBHOOK_SECRET` in Railway.

---

## Retell Setup

### 1. Add Tool: place_order
Retell Dashboard → Agent → Tools → Add Custom Tool:

```json
{
  "name": "place_order",
  "description": "Place the Shopify order when the customer confirms they want to complete the purchase",
  "url": "https://your-railway-url.up.railway.app/retell-tool/order",
  "method": "POST",
  "speak_during_execution": true,
  "speak_after_execution": true,
  "execution_message_description": "placing your order",
  "parameters": {
    "type": "object",
    "properties": {
      "confirmed": {
        "type": "boolean",
        "description": "Set to true when customer explicitly confirms they want to place the order"
      }
    },
    "required": ["confirmed"]
  }
}
```

### 2. Add Tool: decline (optional but recommended)
```json
{
  "name": "decline",
  "description": "Called when customer declines or is not interested",
  "url": "https://your-railway-url.up.railway.app/retell-tool/decline",
  "method": "POST",
  "parameters": {
    "type": "object",
    "properties": {}
  }
}
```

### 3. Update System Prompt

Add this section to your agent's system prompt:

```
TOOLS:
When customer clearly confirms they want to order (says yes, confirmă, da vreau, 
plasează comanda, etc.) → call place_order tool with confirmed=true.
After tool returns → tell customer: "Comanda #{order_number} a fost plasată! 
Veți primi confirmare pe email."

When customer declines → call decline tool, then close naturally.

IMPORTANT: Only call place_order when customer gives CLEAR confirmation.
Do not call it if customer is just asking questions or seems uncertain.
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/shopify-webhook` | Receives Shopify abandoned checkout |
| POST | `/retell-tool/order` | Called by Retell to place order |
| POST | `/retell-tool/decline` | Called by Retell on customer decline |

---

## Test Locally

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your values
python app.py
```

Test webhook with curl:
```bash
curl -X POST http://localhost:5000/shopify-webhook \
  -H "Content-Type: application/json" \
  -H "X-Shopify-Topic: checkouts/update" \
  -d '{
    "checkout": {
      "token": "test123",
      "email": "test@test.com",
      "phone": "0712345678",
      "total_price": "89.00",
      "shipping_address": {
        "first_name": "Ion",
        "last_name": "Popescu",
        "address1": "Str. Exemplu 1",
        "city": "Brasov",
        "zip": "500001",
        "country_code": "RO",
        "phone": "0712345678"
      },
      "line_items": [
        {
          "title": "Gel Cuticule 60ml",
          "variant_id": 123456789,
          "quantity": 1,
          "price": "89.00"
        }
      ]
    }
  }'
```

---

## Production Notes

- `active_calls` is stored in memory — works fine for low volume
- For high volume (100+ concurrent calls): replace with Redis
  - `pip install redis` and swap `active_calls` dict for Redis client
- Add rate limiting if needed: `pip install flask-limiter`
