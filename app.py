import base64
import hmac
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from flask import Flask, Response, flash, redirect, render_template, request, url_for
from twilio.rest import Client


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-this-secret")

BRAND_NAME = os.getenv("BRAND_NAME", "Rudradhan")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "video_call_leads")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

SHEET_HEADERS = [
    "timestamp",
    "name",
    "whatsapp",
    "country",
    "sku",
    "product_url",
    "preferred_time",
    "message",
    "source",
    "campaign",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "referrer",
    "status",
    "notes",
]

STATUS_OPTIONS = ["New", "Contacted", "Call Scheduled", "Completed", "Converted", "Lost"]


def now_text() -> str:
    try:
        tz = ZoneInfo(TIMEZONE)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_service_account_info() -> dict:
    """Load Google service account JSON from either raw JSON or base64 JSON."""
    raw_b64 = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
        or os.getenv("GOOGLE_SHEETS_KEY_B64", "").strip()
    )
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            return json.loads(decoded)
        except Exception as exc:
            raise RuntimeError("Google service account base64 env var is not valid base64 JSON") from exc

    if raw_json:
        try:
            return json.loads(raw_json)
        except Exception as exc:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc

    raise RuntimeError(
        "Missing Google credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SERVICE_ACCOUNT_JSON_B64, or GOOGLE_SHEETS_KEY_B64."
    )


def get_worksheet():
    sheet_id = env_required("GOOGLE_SHEET_ID")
    info = load_service_account_info()
    gc = gspread.service_account_from_dict(info)
    spreadsheet = gc.open_by_key(sheet_id)

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME,
            rows=1000,
            cols=len(SHEET_HEADERS) + 2,
        )
        worksheet.update("A1", [SHEET_HEADERS])
        return worksheet

    first_row = worksheet.row_values(1)
    if first_row[: len(SHEET_HEADERS)] != SHEET_HEADERS:
        worksheet.update("A1", [SHEET_HEADERS])
    return worksheet


def clean_str(value: str, limit: int = 500) -> str:
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:limit]


def clean_url(value: str, limit: int = 1000) -> str:
    return clean_str(value, limit=limit)


def customer_wa_link(phone: str) -> str:
    digits = re.sub(r"[^0-9]", "", phone or "")
    if not digits:
        return ""
    return f"https://wa.me/{digits}"


def build_lead_from_request():
    return {
        "timestamp": now_text(),
        "name": clean_str(request.form.get("name"), 120),
        "whatsapp": clean_str(request.form.get("whatsapp"), 80),
        "country": clean_str(request.form.get("country"), 80),
        "sku": clean_str(request.form.get("sku"), 120),
        "product_url": clean_url(request.form.get("product_url"), 1000),
        "preferred_time": clean_str(request.form.get("preferred_time"), 160),
        "message": clean_str(request.form.get("message"), 1000),
        "source": clean_str(request.form.get("source"), 120),
        "campaign": clean_str(request.form.get("campaign"), 160),
        "utm_source": clean_str(request.form.get("utm_source"), 160),
        "utm_medium": clean_str(request.form.get("utm_medium"), 160),
        "utm_campaign": clean_str(request.form.get("utm_campaign"), 160),
        "referrer": clean_url(request.form.get("referrer"), 1000),
        "status": "New",
        "notes": "",
    }


def validate_lead(lead: dict) -> list[str]:
    errors = []
    if not lead["name"]:
        errors.append("Please enter your name.")
    if not lead["whatsapp"]:
        errors.append("Please enter your WhatsApp number.")
    if not lead["country"]:
        errors.append("Please enter your country.")
    if not lead["preferred_time"]:
        errors.append("Please enter your preferred call time.")
    return errors


def append_lead_to_sheet(lead: dict) -> None:
    worksheet = get_worksheet()
    row = [lead.get(header, "") for header in SHEET_HEADERS]
    worksheet.append_row(row, value_input_option="USER_ENTERED")


def twilio_is_configured() -> bool:
    required = [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "ALERT_WHATSAPP_TO",
    ]

    has_auth = all(os.getenv(name, "").strip() for name in required)
    has_sender = bool(
        os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
        or os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    )
    return has_auth and has_sender


def build_alert_body(lead: dict) -> str:
    wa_link = customer_wa_link(lead.get("whatsapp", ""))
    lines = [
        "New Rudradhan video-call lead",
        f"Name: {lead.get('name', '')}",
        f"WhatsApp: {lead.get('whatsapp', '')}",
        f"Country: {lead.get('country', '')}",
        f"SKU: {lead.get('sku', '') or '-'}",
        f"Preferred time: {lead.get('preferred_time', '')}",
    ]
    if lead.get("product_url"):
        lines.append(f"Product: {lead.get('product_url')}")
    if lead.get("message"):
        lines.append(f"Message: {lead.get('message')}")
    if wa_link:
        lines.append(f"Open WhatsApp: {wa_link}")
    return "\n".join(lines)


def build_twilio_content_variables(lead: dict) -> str:
    """
    Variables for the approved Twilio WhatsApp Content template.

    Recommended template body:

    New Rudradhan video-call lead
    Name: {{1}}
    WhatsApp: {{2}}
    Country: {{3}}
    SKU: {{4}}
    Preferred time: {{5}}
    Product: {{6}}
    Message: {{7}}
    Open WhatsApp: {{8}}
    """
    variables = {
        "1": lead.get("name", "") or "-",
        "2": lead.get("whatsapp", "") or "-",
        "3": lead.get("country", "") or "-",
        "4": lead.get("sku", "") or "-",
        "5": lead.get("preferred_time", "") or "-",
        "6": lead.get("product_url", "") or "-",
        "7": lead.get("message", "") or "-",
        "8": customer_wa_link(lead.get("whatsapp", "")) or "-",
    }
    return json.dumps(variables)


def twilio_sender_kwargs() -> dict:
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    if messaging_service_sid:
        return {"messaging_service_sid": messaging_service_sid}

    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    return {"from_": from_number}


def send_whatsapp_alert(lead: dict) -> None:
    if not twilio_is_configured():
        logger.info("Twilio env vars not fully configured. Skipping WhatsApp alert.")
        return

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    content_sid = os.getenv("TWILIO_CONTENT_SID", "").strip()
    recipients = [x.strip() for x in os.getenv("ALERT_WHATSAPP_TO", "").split(",") if x.strip()]

    client = Client(account_sid, auth_token)
    sender_kwargs = twilio_sender_kwargs()

    for to_number in recipients:
        try:
            if content_sid:
                client.messages.create(
                    **sender_kwargs,
                    to=to_number,
                    content_sid=content_sid,
                    content_variables=build_twilio_content_variables(lead),
                )
            else:
                client.messages.create(
                    **sender_kwargs,
                    to=to_number,
                    body=build_alert_body(lead),
                )
        except Exception:
            logger.exception("Failed to send Twilio WhatsApp alert to %s", to_number)


def get_query_defaults():
    return {
        "sku": clean_str(request.args.get("sku"), 120),
        "product_url": clean_url(request.args.get("product_url"), 1000),
        "source": clean_str(request.args.get("source") or request.args.get("utm_source"), 120),
        "campaign": clean_str(request.args.get("campaign") or request.args.get("utm_campaign"), 160),
        "utm_source": clean_str(request.args.get("utm_source"), 160),
        "utm_medium": clean_str(request.args.get("utm_medium"), 160),
        "utm_campaign": clean_str(request.args.get("utm_campaign"), 160),
    }


def admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "").strip()


def is_admin_authorized() -> bool:
    password = admin_password()
    if not password:
        return False
    auth = request.authorization
    if not auth:
        return False
    username_ok = hmac.compare_digest(auth.username or "", "admin")
    password_ok = hmac.compare_digest(auth.password or "", password)
    return username_ok and password_ok


def require_admin():
    return Response(
        "Admin login required",
        401,
        {"WWW-Authenticate": 'Basic realm="Rudradhan Video Call Admin"'},
    )


@app.route("/healthz")
def healthz():
    return {"ok": True, "service": "rudradhan-video-call-app"}


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        brand_name=BRAND_NAME,
        defaults=get_query_defaults(),
    )


@app.route("/submit", methods=["POST"])
def submit():
    lead = build_lead_from_request()
    errors = validate_lead(lead)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template("index.html", brand_name=BRAND_NAME, defaults=lead), 400

    append_lead_to_sheet(lead)
    send_whatsapp_alert(lead)
    return redirect(url_for("thank_you"))


@app.route("/thank-you", methods=["GET"])
def thank_you():
    return render_template("thank_you.html", brand_name=BRAND_NAME)


@app.route("/admin", methods=["GET"])
def admin():
    if not is_admin_authorized():
        return require_admin()

    worksheet = get_worksheet()
    records = worksheet.get_all_records(expected_headers=SHEET_HEADERS)
    leads = []
    for idx, row in enumerate(records, start=2):
        item = dict(row)
        item["row_number"] = idx
        item["customer_wa_link"] = customer_wa_link(item.get("whatsapp", ""))
        leads.append(item)
    leads.reverse()

    return render_template(
        "admin.html",
        brand_name=BRAND_NAME,
        leads=leads,
        status_options=STATUS_OPTIONS,
    )


@app.route("/admin/update", methods=["POST"])
def admin_update():
    if not is_admin_authorized():
        return require_admin()

    row_number = int(request.form.get("row_number", "0"))
    status = clean_str(request.form.get("status"), 80)
    notes = clean_str(request.form.get("notes"), 1000)

    if row_number < 2:
        flash("Invalid row number.", "error")
        return redirect(url_for("admin"))

    if status not in STATUS_OPTIONS:
        flash("Invalid status.", "error")
        return redirect(url_for("admin"))

    worksheet = get_worksheet()
    status_col = SHEET_HEADERS.index("status") + 1
    notes_col = SHEET_HEADERS.index("notes") + 1
    worksheet.update_cell(row_number, status_col, status)
    worksheet.update_cell(row_number, notes_col, notes)

    flash("Lead updated.", "success")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
