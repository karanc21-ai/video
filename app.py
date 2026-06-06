import base64
import json
import logging
import os
import re
from datetime import datetime, date, time, timedelta
from functools import wraps
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import gspread
import requests
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google.oauth2.service_account import Credentials
from twilio.rest import Client as TwilioClient
from zoneinfo import ZoneInfo


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")
logging.basicConfig(level=logging.INFO)


# -----------------------------
# Basic config
# -----------------------------
BRAND_NAME = os.getenv("BRAND_NAME", "Rudradhan").strip() or "Rudradhan"
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "video_call_leads").strip() or "video_call_leads"
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

BOOKING_TIMEZONE = os.getenv("BOOKING_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
BOOKING_DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "14"))
BOOKING_SLOT_START = os.getenv("BOOKING_SLOT_START", "16:00").strip()
BOOKING_SLOT_END = os.getenv("BOOKING_SLOT_END", "19:30").strip()
BOOKING_SLOT_INTERVAL_MINUTES = int(os.getenv("BOOKING_SLOT_INTERVAL_MINUTES", "10"))
BOOKING_MIN_LEAD_MINUTES = int(os.getenv("BOOKING_MIN_LEAD_MINUTES", "60"))

STORE_VISIT_LOCATION_NAME = os.getenv("STORE_VISIT_LOCATION_NAME", "Rudradhan Amritsar Store").strip()
STORE_VISIT_ADDRESS = os.getenv("STORE_VISIT_ADDRESS", "42 Mall Road, Amritsar 143001").strip()
STORE_VISIT_MAP_URL = os.getenv("STORE_VISIT_MAP_URL", "").strip()
STORE_VISIT_SLOT_START = os.getenv("STORE_VISIT_SLOT_START", "13:00").strip()
# Last in-store appointment can start at 6:00 PM.
STORE_VISIT_SLOT_END = os.getenv("STORE_VISIT_SLOT_END", "18:00").strip()
STORE_VISIT_SLOT_INTERVAL_MINUTES = int(os.getenv("STORE_VISIT_SLOT_INTERVAL_MINUTES", "30"))

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-07").strip() or "2025-07"
DEFAULT_STORE = os.getenv("DEFAULT_STORE", "in").strip().lower() or "in"
READY_METAFIELD_NAMESPACE = os.getenv("READY_METAFIELD_NAMESPACE", "custom").strip()
READY_METAFIELD_KEY = os.getenv("READY_METAFIELD_KEY", "delivery_time").strip()
READY_METAFIELD_VALUE = os.getenv("READY_METAFIELD_VALUE", "2-5 Days Across India").strip()
REQUIRE_READY_TO_SHIP = os.getenv("REQUIRE_READY_TO_SHIP", "true").strip().lower() in {"1", "true", "yes", "y"}
READY_PRODUCT_FETCH_LIMIT = int(os.getenv("READY_PRODUCT_FETCH_LIMIT", "100"))

DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "India").strip() or "India"
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "+91").strip() or "+91"

COUNTRY_CODE_OPTIONS = [
    {"code": "+91", "label": "India (+91)"},
    {"code": "+1", "label": "USA / Canada (+1)"},
    {"code": "+44", "label": "United Kingdom (+44)"},
    {"code": "+61", "label": "Australia (+61)"},
    {"code": "+65", "label": "Singapore (+65)"},
    {"code": "+971", "label": "UAE (+971)"},
    {"code": "+966", "label": "Saudi Arabia (+966)"},
    {"code": "+974", "label": "Qatar (+974)"},
    {"code": "+965", "label": "Kuwait (+965)"},
    {"code": "+973", "label": "Bahrain (+973)"},
    {"code": "+968", "label": "Oman (+968)"},
    {"code": "+60", "label": "Malaysia (+60)"},
    {"code": "+852", "label": "Hong Kong (+852)"},
    {"code": "+64", "label": "New Zealand (+64)"},
    {"code": "+49", "label": "Germany (+49)"},
    {"code": "+33", "label": "France (+33)"},
]

HEADERS = [
    "timestamp",
    "name",
    "whatsapp",
    "country",
    "appointment_date",
    "appointment_time",
    "appointment_datetime",
    "check_actual_product",
    "check_size_scale",
    "check_color_shine",
    "check_weight_comfort",
    "check_styling",
    "check_ready_to_ship",
    "message",
    "store",
    "product_title",
    "sku",
    "product_handle",
    "product_url",
    "product_image_url",
    "product_image",
    "source",
    "campaign",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "referrer",
    "status",
    "notes",
    "country_code",
    "phone_number",
    "appointment_type",
    "appointment_label",
    "appointment_location",
]

CHECKBOX_KEYS = {
    "actual_product": "Actual product clarity",
    "size_scale": "Size / scale",
    "color_shine": "Color and shine",
    "weight_comfort": "Weight / comfort",
    "styling": "Styling advice",
    "ready_to_ship": "Ready-to-ship availability",
}

STORE_VISIT_CHECKBOX_KEYS = {
    "actual_product": "See multiple products in person",
    "size_scale": "Check size / scale",
    "color_shine": "Check color and shine",
    "weight_comfort": "Try weight / comfort",
    "styling": "Styling guidance",
    "ready_to_ship": "Ready-to-buy options",
}


def normalize_appointment_type(value):
    raw = (value or "video_call").strip().lower().replace("-", "_")
    if raw in {"store", "store_visit", "storevisit", "in_store", "in_store_visit", "visit"}:
        return "store_visit"
    return "video_call"


def get_store_visit_map_url():
    if STORE_VISIT_MAP_URL:
        return STORE_VISIT_MAP_URL
    if STORE_VISIT_ADDRESS:
        return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(STORE_VISIT_ADDRESS)
    return ""


def get_appointment_config(appointment_type):
    appointment_type = normalize_appointment_type(appointment_type)
    if appointment_type == "store_visit":
        return {
            "type": "store_visit",
            "label": "Store Visit",
            "subtitle": "Amritsar store appointment",
            "page_title": "Store Visit Appointment",
            "eyebrow": "Visit by appointment",
            "headline": "Book a visit to our Amritsar store.",
            "lead_text": "Choose a convenient slot to visit us, see multiple jewellery pieces, compare size, finish, color, and styling in person.",
            "trust_points": ["Multiple products", "In-person viewing", "WhatsApp confirmation"],
            "details_note": "Available Monday to Saturday, 1:00 PM to 6:00 PM India time.",
            "check_title": "What would you like to see during your visit?",
            "message_placeholder": "Example: I am looking for necklace sets and earrings for a wedding, preferably pearl and jadau styles.",
            "consent_text": "By submitting, you agree that Rudradhan may contact you on WhatsApp about this store-visit request.",
            "button_text": "Book Store Visit",
            "thank_you_subtitle": "Amritsar store appointment",
            "thank_you_contact_text": "Our team will contact you on WhatsApp to confirm your store visit.",
            "slot_start": STORE_VISIT_SLOT_START,
            "slot_end": STORE_VISIT_SLOT_END,
            "slot_interval_minutes": STORE_VISIT_SLOT_INTERVAL_MINUTES,
            "checkbox_keys": STORE_VISIT_CHECKBOX_KEYS,
            "default_checks": ["actual_product", "size_scale", "color_shine"],
            "requires_product": False,
            "requires_ready_to_ship": False,
            "location_name": STORE_VISIT_LOCATION_NAME,
            "location_address": STORE_VISIT_ADDRESS,
            "location_map_url": get_store_visit_map_url(),
        }

    return {
        "type": "video_call",
        "label": "Video Call",
        "subtitle": "Video shopping appointment",
        "page_title": "Video Shopping Appointment",
        "eyebrow": "Ready-to-ship jewellery, shown live",
        "headline": "See this piece live before buying.",
        "lead_text": "Book a short 10-minute video call to check size, color, shine, finish, and actual product clarity with our team.",
        "trust_points": ["Actual product clarity", "WhatsApp follow-up", "Ready-to-ship friendly"],
        "details_note": "Available Monday to Saturday, 4:00 PM to 7:30 PM India time.",
        "check_title": "What do you want to check on video?",
        "message_placeholder": "Example: I want to see how large the earrings look, or how bright the stones are in normal light.",
        "consent_text": "By submitting, you agree that Rudradhan may contact you on WhatsApp about this video-call request.",
        "button_text": "Book 10-Minute Video Call",
        "thank_you_subtitle": "Video shopping appointment",
        "thank_you_contact_text": "Our team will contact you on WhatsApp to confirm the video call.",
        "slot_start": BOOKING_SLOT_START,
        "slot_end": BOOKING_SLOT_END,
        "slot_interval_minutes": BOOKING_SLOT_INTERVAL_MINUTES,
        "checkbox_keys": CHECKBOX_KEYS,
        "default_checks": ["actual_product", "size_scale", "color_shine"],
        "requires_product": True,
        "requires_ready_to_ship": REQUIRE_READY_TO_SHIP,
        "location_name": "",
        "location_address": "",
        "location_map_url": "",
    }


# -----------------------------
# Google Sheets
# -----------------------------
def load_service_account_info():
    raw_b64 = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
        or os.getenv("GOOGLE_SHEETS_KEY_B64", "").strip()
    )

    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            return json.loads(decoded)
        except Exception as exc:
            raise RuntimeError("Base64 Google service account key is invalid") from exc

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc

    raise RuntimeError(
        "Missing Google credentials. Set GOOGLE_SHEETS_KEY_B64, "
        "GOOGLE_SERVICE_ACCOUNT_JSON_B64, or GOOGLE_SERVICE_ACCOUNT_JSON."
    )


def get_worksheet():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is missing")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(load_service_account_info(), scopes=scopes)
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADERS) + 5)

    existing_headers = worksheet.row_values(1)
    if existing_headers != HEADERS:
        worksheet.update("A1", [HEADERS])
    return worksheet


def row_to_dict(row):
    data = {}
    for idx, header in enumerate(HEADERS):
        data[header] = row[idx] if idx < len(row) else ""
    return data


def get_all_leads():
    worksheet = get_worksheet()
    rows = worksheet.get_all_values()[1:]
    leads = []
    for i, row in enumerate(rows, start=2):
        lead = row_to_dict(row)
        lead["row_number"] = i
        lead["appointment_type"] = normalize_appointment_type(lead.get("appointment_type"))
        if not lead.get("appointment_label"):
            lead["appointment_label"] = get_appointment_config(lead["appointment_type"])["label"]
        leads.append(lead)
    leads.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return leads


def append_lead_to_sheet(lead):
    worksheet = get_worksheet()
    image_url = lead.get("product_image_url", "").strip()
    image_formula = f'=IMAGE("{image_url}")' if image_url else ""

    row = [
        lead.get("timestamp", ""),
        lead.get("name", ""),
        lead.get("whatsapp", ""),
        lead.get("country", ""),
        lead.get("appointment_date", ""),
        lead.get("appointment_time", ""),
        lead.get("appointment_datetime", ""),
        lead.get("check_actual_product", ""),
        lead.get("check_size_scale", ""),
        lead.get("check_color_shine", ""),
        lead.get("check_weight_comfort", ""),
        lead.get("check_styling", ""),
        lead.get("check_ready_to_ship", ""),
        lead.get("message", ""),
        lead.get("store", ""),
        lead.get("product_title", ""),
        lead.get("sku", ""),
        lead.get("product_handle", ""),
        lead.get("product_url", ""),
        image_url,
        image_formula,
        lead.get("source", ""),
        lead.get("campaign", ""),
        lead.get("utm_source", ""),
        lead.get("utm_medium", ""),
        lead.get("utm_campaign", ""),
        lead.get("referrer", ""),
        lead.get("status", "New"),
        lead.get("notes", ""),
        lead.get("country_code", ""),
        lead.get("phone_number", ""),
        lead.get("appointment_type", "video_call"),
        lead.get("appointment_label", "Video Call"),
        lead.get("appointment_location", ""),
    ]
    worksheet.append_row(row, value_input_option="USER_ENTERED")


def update_lead_status(row_number, status, notes=""):
    worksheet = get_worksheet()
    if row_number < 2:
        raise ValueError("Invalid row number")
    status_col = HEADERS.index("status") + 1
    notes_col = HEADERS.index("notes") + 1
    worksheet.update_cell(row_number, status_col, status)
    if notes:
        worksheet.update_cell(row_number, notes_col, notes)


def get_booked_slots(appointment_type="video_call"):
    """Return set of (appointment_date, appointment_time) already taken for one appointment type."""
    appointment_type = normalize_appointment_type(appointment_type)
    try:
        leads = get_all_leads()
    except Exception as exc:
        app.logger.warning("Could not read booked slots from sheet: %s", exc)
        return set()

    booked = set()
    for lead in leads:
        status = (lead.get("status") or "").strip().lower()
        if status in {"lost", "cancelled", "canceled"}:
            continue
        lead_type = normalize_appointment_type(lead.get("appointment_type"))
        if lead_type != appointment_type:
            continue
        d = (lead.get("appointment_date") or "").strip()
        t = (lead.get("appointment_time") or "").strip()
        if d and t:
            booked.add((d, t))
    return booked


# -----------------------------
# Shopify
# -----------------------------
def get_store_config(store_key):
    store = (store_key or DEFAULT_STORE).strip().lower()
    if store in {"us", "usa", "rudradhan_us"}:
        domain = os.getenv("SHOPIFY_US_SHOP_DOMAIN", "").strip()
        token = os.getenv("SHOPIFY_US_ADMIN_ACCESS_TOKEN", "").strip()
        return {"store": "us", "domain": domain, "token": token}

    domain = os.getenv("SHOPIFY_IN_SHOP_DOMAIN", "").strip()
    token = os.getenv("SHOPIFY_IN_ADMIN_ACCESS_TOKEN", "").strip()
    return {"store": "in", "domain": domain, "token": token}


def shopify_graphql(store_key, query, variables=None):
    cfg = get_store_config(store_key)
    if not cfg["domain"] or not cfg["token"]:
        raise RuntimeError(f"Shopify config missing for store '{cfg['store']}'")

    url = f"https://{cfg['domain']}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    response = requests.post(
        url,
        headers={
            "X-Shopify-Access-Token": cfg["token"],
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")
    return payload["data"]


PRODUCT_FRAGMENT = """
fragment ProductBookingFields on Product {
  id
  title
  handle
  status
  onlineStoreUrl
  featuredMedia {
    preview { image { url altText } }
  }
  metafield(namespace: $readyNamespace, key: $readyKey) { value }
  variants(first: 25) {
    edges {
      node {
        id
        sku
        inventoryQuantity
        image { url altText }
      }
    }
  }
}
"""


def normalize_product(node, store_key, requested_sku=""):
    if not node:
        return None

    variants = [edge["node"] for edge in node.get("variants", {}).get("edges", [])]
    chosen_variant = None
    if requested_sku:
        for v in variants:
            if (v.get("sku") or "").strip().lower() == requested_sku.strip().lower():
                chosen_variant = v
                break
    if not chosen_variant and variants:
        chosen_variant = next((v for v in variants if (v.get("sku") or "").strip()), variants[0])

    featured_image = ""
    if node.get("featuredMedia"):
        featured_image = (((node["featuredMedia"] or {}).get("preview") or {}).get("image") or {}).get("url") or ""

    variant_image = ""
    if chosen_variant and chosen_variant.get("image"):
        variant_image = (chosen_variant.get("image") or {}).get("url") or ""

    image_url = variant_image or featured_image
    metafield_value = ((node.get("metafield") or {}).get("value") or "").strip()
    ready = metafield_value == READY_METAFIELD_VALUE
    online_url = node.get("onlineStoreUrl") or ""

    return {
        "store": store_key or DEFAULT_STORE,
        "title": node.get("title") or "",
        "handle": node.get("handle") or "",
        "status": node.get("status") or "",
        "sku": (chosen_variant or {}).get("sku") or requested_sku or "",
        "product_url": online_url,
        "image_url": image_url,
        "ready_metafield_value": metafield_value,
        "is_ready_to_ship": ready,
    }


def fetch_product_by_handle(store_key, handle):
    if not handle:
        return None
    query = PRODUCT_FRAGMENT + """
query ProductByHandle($productQuery: String!, $readyNamespace: String!, $readyKey: String!) {
  products(first: 1, query: $productQuery) {
    edges { node { ...ProductBookingFields } }
  }
}
"""
    data = shopify_graphql(
        store_key,
        query,
        {
            "productQuery": f"handle:{handle}",
            "readyNamespace": READY_METAFIELD_NAMESPACE,
            "readyKey": READY_METAFIELD_KEY,
        },
    )
    edges = data.get("products", {}).get("edges", [])
    return normalize_product(edges[0]["node"], store_key) if edges else None


def fetch_product_by_sku(store_key, sku):
    if not sku:
        return None
    query = PRODUCT_FRAGMENT + """
query ProductBySku($variantQuery: String!, $readyNamespace: String!, $readyKey: String!) {
  productVariants(first: 1, query: $variantQuery) {
    edges {
      node {
        sku
        product { ...ProductBookingFields }
      }
    }
  }
}
"""
    data = shopify_graphql(
        store_key,
        query,
        {
            "variantQuery": f"sku:{sku}",
            "readyNamespace": READY_METAFIELD_NAMESPACE,
            "readyKey": READY_METAFIELD_KEY,
        },
    )
    edges = data.get("productVariants", {}).get("edges", [])
    if not edges:
        return None
    variant_sku = edges[0]["node"].get("sku") or sku
    return normalize_product(edges[0]["node"].get("product"), store_key, requested_sku=variant_sku)


def fetch_product_from_request():
    store = request.args.get("store", DEFAULT_STORE).strip().lower() or DEFAULT_STORE
    handle = (request.args.get("handle") or "").strip()
    sku = (request.args.get("sku") or "").strip()
    product_url = (request.args.get("product_url") or "").strip()

    if not handle and product_url:
        handle = extract_handle_from_url(product_url) or ""

    product = None
    error = ""
    try:
        if handle:
            product = fetch_product_by_handle(store, handle)
        elif sku:
            product = fetch_product_by_sku(store, sku)
    except Exception as exc:
        app.logger.exception("Shopify product lookup failed")
        error = str(exc)

    if product and product_url and not product.get("product_url"):
        product["product_url"] = product_url

    return product, error


def fetch_ready_products(store_key, search=""):
    query_text = "status:active"
    if search:
        # Shopify product search query supports terms; keep this simple.
        query_text = f"status:active {search}"

    gql = PRODUCT_FRAGMENT + """
query ReadyProducts($productQuery: String!, $readyNamespace: String!, $readyKey: String!, $first: Int!) {
  products(first: $first, query: $productQuery) {
    edges { node { ...ProductBookingFields } }
  }
}
"""
    first = max(1, min(READY_PRODUCT_FETCH_LIMIT, 250))
    data = shopify_graphql(
        store_key,
        gql,
        {
            "productQuery": query_text,
            "readyNamespace": READY_METAFIELD_NAMESPACE,
            "readyKey": READY_METAFIELD_KEY,
            "first": first,
        },
    )
    products = []
    for edge in data.get("products", {}).get("edges", []):
        p = normalize_product(edge["node"], store_key)
        if p and p.get("is_ready_to_ship"):
            products.append(p)
    return products


def extract_handle_from_url(value):
    try:
        parsed = urlparse(value)
        path = parsed.path or ""
    except Exception:
        path = value or ""
    match = re.search(r"/products/([^/?#]+)", path)
    if match:
        return match.group(1)
    return ""


def build_book_url(product, source="instagram_story", campaign="ready_to_ship_video_call", appointment_type="video_call"):
    base = request.url_root.rstrip("/") + url_for("book")
    params = {
        "appointment_type": normalize_appointment_type(appointment_type),
        "store": product.get("store") or DEFAULT_STORE,
        "handle": product.get("handle") or "",
        "source": source,
        "campaign": campaign,
    }
    sku = product.get("sku") or ""
    if sku:
        params["sku"] = sku
    return base + "?" + urlencode(params)


# -----------------------------
# Booking slots
# -----------------------------
def parse_hhmm(value):
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def format_time_ampm(t):
    hour = t.hour
    minute = t.minute
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def generate_booking_dates_and_slots(appointment_type="video_call"):
    appointment_config = get_appointment_config(appointment_type)
    tz = ZoneInfo(BOOKING_TIMEZONE)
    now = datetime.now(tz)
    start_t = parse_hhmm(appointment_config["slot_start"])
    end_t = parse_hhmm(appointment_config["slot_end"])
    interval = timedelta(minutes=appointment_config["slot_interval_minutes"])
    min_lead = timedelta(minutes=BOOKING_MIN_LEAD_MINUTES)
    booked = get_booked_slots(appointment_config["type"])

    days = []
    for offset in range(0, BOOKING_DAYS_AHEAD + 1):
        d = (now + timedelta(days=offset)).date()
        # Monday=0 ... Saturday=5, Sunday=6
        if d.weekday() == 6:
            continue

        slots = []
        current_dt = datetime.combine(d, start_t, tzinfo=tz)
        end_dt = datetime.combine(d, end_t, tzinfo=tz)
        while current_dt <= end_dt:
            value_date = d.isoformat()
            value_time = current_dt.strftime("%H:%M")
            if current_dt >= now + min_lead and (value_date, value_time) not in booked:
                slots.append({"value": value_time, "label": format_time_ampm(current_dt.time())})
            current_dt += interval

        if slots:
            days.append(
                {
                    "value": d.isoformat(),
                    "label": d.strftime("%a, %d %b %Y"),
                    "slots": slots,
                }
            )
    return days


def is_valid_slot(appointment_date, appointment_time, appointment_type="video_call"):
    days = generate_booking_dates_and_slots(appointment_type)
    for day in days:
        if day["value"] == appointment_date:
            return any(slot["value"] == appointment_time for slot in day["slots"])
    return False


def appointment_display(appointment_date, appointment_time):
    if not appointment_date or not appointment_time:
        return ""
    try:
        d = date.fromisoformat(appointment_date)
        hh, mm = appointment_time.split(":")
        t = time(int(hh), int(mm))
        return f"{d.strftime('%a, %d %b %Y')} at {format_time_ampm(t)}"
    except Exception:
        return f"{appointment_date} {appointment_time}"



# -----------------------------
# Phone helpers
# -----------------------------
def normalize_country_code(country_code):
    """Return country code as +NN, allowing pasted values with spaces or punctuation."""
    raw = (country_code or DEFAULT_COUNTRY_CODE or "+91").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return "+91"
    return "+" + digits


def normalize_local_phone_number(phone_number):
    """Allow spaces/dashes/brackets in user input and return only digits."""
    return re.sub(r"\D", "", phone_number or "")


def build_twilio_whatsapp_number(country_code, phone_number):
    """
    Build whatsapp:+E164 from a separate country-code dropdown and phone input.

    Accepts phone numbers with spaces, dashes, brackets, or a full +country-code prefix.
    If the customer types a full international number beginning with +, the typed number wins.
    """
    raw_phone = (phone_number or "").strip()
    if not raw_phone:
        return ""
    if raw_phone.startswith("whatsapp:"):
        return raw_phone

    compact = re.sub(r"[\s().-]+", "", raw_phone)
    if compact.startswith("+"):
        digits = re.sub(r"\D", "", compact)
        return "whatsapp:+" + digits if digits else ""

    digits = normalize_local_phone_number(raw_phone).lstrip("0")
    if not digits:
        return ""

    return "whatsapp:" + normalize_country_code(country_code) + digits

# -----------------------------
# Twilio WhatsApp
# -----------------------------
def twilio_sender_kwargs():
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    whatsapp_from = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    if messaging_service_sid:
        return {"messaging_service_sid": messaging_service_sid}
    if whatsapp_from:
        return {"from_": whatsapp_from}
    return {}


def send_twilio_message(to_number, body=None, content_sid=None, content_variables=None):
    """
    Send WhatsApp message through Twilio.

    If content_sid is supplied, this sends a Twilio Content Template message.
    Do not send free-text body together with content_sid.
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token or not to_number:
        app.logger.info("Twilio skipped: missing SID/token/to_number")
        return None

    sender = twilio_sender_kwargs()
    if not sender:
        app.logger.warning("Twilio skipped: missing TWILIO_WHATSAPP_FROM or TWILIO_MESSAGING_SERVICE_SID")
        return None

    client = TwilioClient(sid, token)
    kwargs = {"to": to_number, **sender}

    if content_sid:
        kwargs["content_sid"] = content_sid
        if content_variables:
            kwargs["content_variables"] = json.dumps(
                {str(k): str(v or "") for k, v in content_variables.items()}
            )
    else:
        if not body:
            app.logger.warning("Twilio skipped: no body/content_sid supplied")
            return None
        kwargs["body"] = body

    msg = client.messages.create(**kwargs)
    app.logger.info(
        "Twilio message created: sid=%s status=%s to=%s content_sid=%s",
        getattr(msg, "sid", None),
        getattr(msg, "status", None),
        to_number,
        content_sid or "",
    )
    return msg


def send_internal_alert(lead):
    """
    Send internal team alert.

    Recommended Twilio/Meta template, no variables:
    New Rudradhan appointment request received. Please open the admin dashboard
    to review the request and contact the customer.
    """
    alert_to = os.getenv("ALERT_WHATSAPP_TO", "").strip()
    if not alert_to:
        app.logger.info("ALERT_WHATSAPP_TO not set. Skipping internal alert.")
        return

    recipients = [x.strip() for x in alert_to.split(",") if x.strip()]
    content_sid = (
        os.getenv("TWILIO_ALERT_CONTENT_SID", "").strip()
        or os.getenv("TWILIO_CONTENT_SID", "").strip()
    )

    dashboard_url = request.url_root.rstrip("/") + url_for("admin")
    appointment_label = lead.get("appointment_label") or get_appointment_config(lead.get("appointment_type"))["label"]
    fallback_body = (
        f"New Rudradhan {appointment_label.lower()} request received. "
        "Please open the admin dashboard to review the request and contact the customer.\n"
        f"{dashboard_url}"
    )

    for to_number in recipients:
        try:
            if content_sid:
                send_twilio_message(to_number=to_number, content_sid=content_sid)
            else:
                send_twilio_message(to_number=to_number, body=fallback_body)
        except Exception as exc:
            app.logger.exception("Twilio internal alert failed for %s: %s", to_number, exc)


def format_customer_date(value):
    try:
        d = date.fromisoformat(value)
        return d.strftime("%d %b %Y")
    except Exception:
        return value or ""


def format_customer_time(value):
    try:
        hh, mm = value.split(":", 1)
        return format_time_ampm(time(int(hh), int(mm)))
    except Exception:
        return value or ""


def send_customer_confirmation(lead):
    """
    Optional customer confirmation.

    Recommended template:
    Hi {{1}}, your Rudradhan appointment request for {{2}} on {{3}} at {{4}}
    has been received. Our team will contact you on WhatsApp if any change is needed.
    """
    content_sid = os.getenv("TWILIO_CUSTOMER_CONTENT_SID", "").strip()
    if not content_sid:
        return

    to_number = normalize_whatsapp_for_twilio(lead.get("whatsapp", ""), lead.get("country", ""))
    if not to_number:
        app.logger.info("Customer confirmation skipped: customer WhatsApp missing/invalid")
        return

    appointment_type = normalize_appointment_type(lead.get("appointment_type"))
    if appointment_type == "store_visit":
        product_label = "your Amritsar store visit"
    else:
        product_label = lead.get("product_title") or lead.get("sku") or "your selected jewellery"
    variables = {
        "1": lead.get("name", "") or "there",
        "2": product_label,
        "3": format_customer_date(lead.get("appointment_date", "")),
        "4": format_customer_time(lead.get("appointment_time", "")),
    }

    try:
        send_twilio_message(to_number=to_number, content_sid=content_sid, content_variables=variables)
    except Exception as exc:
        app.logger.exception("Twilio customer confirmation failed: %s", exc)


def normalize_whatsapp_for_twilio(phone, country=""):
    p = (phone or "").strip()
    if not p:
        return ""
    if p.startswith("whatsapp:"):
        return p
    digits = re.sub(r"[^0-9+]", "", p)
    if digits.startswith("+"):
        return "whatsapp:" + digits
    if len(digits) == 10 and (country or "").strip().lower() in {"", "india", "in"}:
        return "whatsapp:+91" + digits
    if digits.startswith("91") and len(digits) == 12:
        return "whatsapp:+" + digits
    return "whatsapp:+" + digits if digits else ""

# -----------------------------
# Auth
# -----------------------------
def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if session.get("admin_ok"):
            return view_func(*args, **kwargs)
        return redirect(url_for("admin_login", next=request.path))

    return wrapper


# -----------------------------
# Routes
# -----------------------------
@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/")
def index():
    return redirect(url_for("book", **request.args.to_dict()))


@app.route("/book")
def book():
    appointment_type = normalize_appointment_type(request.args.get("appointment_type", "video_call"))
    appointment_config = get_appointment_config(appointment_type)

    product = None
    product_error = ""
    if appointment_config["requires_product"]:
        product, product_error = fetch_product_from_request()

    booking_days = generate_booking_dates_and_slots(appointment_type)
    query_defaults = {
        "appointment_type": appointment_type,
        "store": request.args.get("store", DEFAULT_STORE),
        "handle": request.args.get("handle", ""),
        "sku": request.args.get("sku", ""),
        "source": request.args.get("source", ""),
        "campaign": request.args.get("campaign", ""),
        "utm_source": request.args.get("utm_source", ""),
        "utm_medium": request.args.get("utm_medium", ""),
        "utm_campaign": request.args.get("utm_campaign", ""),
        "product_url": request.args.get("product_url", ""),
        "referrer": request.headers.get("Referer", ""),
    }
    can_book = bool(booking_days)
    if appointment_config["requires_product"] and not product:
        can_book = False
    not_ready = False
    if product and appointment_config["requires_ready_to_ship"] and not product.get("is_ready_to_ship"):
        can_book = False
        not_ready = True

    return render_template(
        "index.html",
        brand_name=BRAND_NAME,
        appointment_type=appointment_type,
        appointment_config=appointment_config,
        product=product,
        product_error=product_error,
        defaults=query_defaults,
        booking_days=booking_days,
        booking_days_json=json.dumps(booking_days),
        checkbox_keys=appointment_config["checkbox_keys"],
        checkbox_defaults=appointment_config["default_checks"],
        can_book=can_book,
        not_ready=not_ready,
        default_country=DEFAULT_COUNTRY,
        default_country_code=normalize_country_code(DEFAULT_COUNTRY_CODE),
        country_code_options=COUNTRY_CODE_OPTIONS,
    )


@app.route("/submit", methods=["POST"])
def submit():
    appointment_type = normalize_appointment_type(request.form.get("appointment_type", "video_call"))
    appointment_config = get_appointment_config(appointment_type)
    appointment_date = request.form.get("appointment_date", "").strip()
    appointment_time = request.form.get("appointment_time", "").strip()
    if not is_valid_slot(appointment_date, appointment_time, appointment_type):
        flash("That appointment slot is no longer available. Please choose another time.", "error")
        return redirect(request.referrer or url_for("book"))

    checks = set(request.form.getlist("checks"))
    now = datetime.now(ZoneInfo(BOOKING_TIMEZONE)).isoformat(timespec="seconds")
    appt_text = appointment_display(appointment_date, appointment_time)

    product_title = request.form.get("product_title", "").strip()
    product_handle = request.form.get("product_handle", "").strip()
    sku = request.form.get("sku", "").strip()
    store = request.form.get("store", DEFAULT_STORE).strip().lower() or DEFAULT_STORE
    country_code = normalize_country_code(request.form.get("country_code", DEFAULT_COUNTRY_CODE))
    phone_number = request.form.get("phone_number", "").strip()
    whatsapp_number = build_twilio_whatsapp_number(country_code, phone_number)

    # Re-validate ready-to-ship where possible. Hidden fields are for convenience, not trust.
    if appointment_config["requires_ready_to_ship"] and (product_handle or sku):
        try:
            product = fetch_product_by_handle(store, product_handle) if product_handle else fetch_product_by_sku(store, sku)
            if product and not product.get("is_ready_to_ship"):
                flash("This item is not currently marked ready-to-ship for video booking.", "error")
                return redirect(request.referrer or url_for("book"))
        except Exception as exc:
            app.logger.warning("Ready-to-ship revalidation skipped: %s", exc)

    lead = {
        "timestamp": now,
        "name": request.form.get("name", "").strip(),
        "whatsapp": whatsapp_number,
        "country": request.form.get("country", "").strip(),
        "country_code": country_code,
        "phone_number": phone_number,
        "appointment_date": appointment_date,
        "appointment_time": appointment_time,
        "appointment_datetime": appt_text,
        "appointment_display": appt_text,
        "check_actual_product": "Yes" if "actual_product" in checks else "",
        "check_size_scale": "Yes" if "size_scale" in checks else "",
        "check_color_shine": "Yes" if "color_shine" in checks else "",
        "check_weight_comfort": "Yes" if "weight_comfort" in checks else "",
        "check_styling": "Yes" if "styling" in checks else "",
        "check_ready_to_ship": "Yes" if "ready_to_ship" in checks else "",
        "message": request.form.get("message", "").strip(),
        "store": store,
        "product_title": product_title,
        "sku": sku,
        "product_handle": product_handle,
        "product_url": request.form.get("product_url", "").strip(),
        "product_image_url": request.form.get("product_image_url", "").strip(),
        "source": request.form.get("source", "").strip(),
        "campaign": request.form.get("campaign", "").strip(),
        "utm_source": request.form.get("utm_source", "").strip(),
        "utm_medium": request.form.get("utm_medium", "").strip(),
        "utm_campaign": request.form.get("utm_campaign", "").strip(),
        "referrer": request.form.get("referrer", "").strip() or request.headers.get("Referer", ""),
        "status": "New",
        "notes": "",
        "appointment_type": appointment_type,
        "appointment_label": appointment_config["label"],
        "appointment_location": appointment_config.get("location_address", "") if appointment_type == "store_visit" else "",
    }

    required = ["name", "phone_number", "country"]
    missing = [key for key in required if not lead.get(key)]
    if missing:
        flash("Please fill name, WhatsApp number, and country.", "error")
        return redirect(request.referrer or url_for("book"))

    append_lead_to_sheet(lead)
    send_internal_alert(lead)
    send_customer_confirmation(lead)
    return redirect(url_for("thank_you", appt=appt_text, appointment_type=appointment_type))


@app.route("/thank-you")
def thank_you():
    appointment_type = normalize_appointment_type(request.args.get("appointment_type", "video_call"))
    return render_template(
        "thank_you.html",
        brand_name=BRAND_NAME,
        appointment=request.args.get("appt", ""),
        appointment_config=get_appointment_config(appointment_type),
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and password == ADMIN_PASSWORD:
            session["admin_ok"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        flash("Wrong password.", "error")
    return render_template("login.html", brand_name=BRAND_NAME)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin():
    leads = get_all_leads()
    return render_template("admin.html", brand_name=BRAND_NAME, leads=leads)


@app.route("/admin/update-status", methods=["POST"])
@admin_required
def admin_update_status():
    row_number = int(request.form.get("row_number", "0"))
    status = request.form.get("status", "New").strip()
    notes = request.form.get("notes", "").strip()
    update_lead_status(row_number, status, notes)
    return redirect(url_for("admin"))


@app.route("/admin/products")
@admin_required
def admin_products():
    store = request.args.get("store", DEFAULT_STORE).strip().lower() or DEFAULT_STORE
    search = request.args.get("q", "").strip()
    products = []
    error = ""
    try:
        products = fetch_ready_products(store, search=search)
    except Exception as exc:
        app.logger.exception("Ready products fetch failed")
        error = str(exc)

    for p in products:
        p["story_link"] = build_book_url(p, source="instagram_story", campaign="ready_to_ship_video_call")
        p["product_page_link"] = build_book_url(p, source="product_page", campaign="ready_to_ship_video_call")

    store_visit_link = request.url_root.rstrip("/") + url_for("book") + "?" + urlencode(
        {
            "appointment_type": "store_visit",
            "source": "product_page",
            "campaign": "amritsar_store_visit",
        }
    )

    return render_template(
        "products.html",
        brand_name=BRAND_NAME,
        products=products,
        store=store,
        search=search,
        error=error,
        store_visit_link=store_visit_link,
    )


@app.template_filter("yesno")
def yesno_filter(value):
    return "Yes" if value else "No"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
