import os
import re
import smtplib
import requests
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

API_KEY        = os.getenv("API_KEY")
API_URL        = os.getenv("API_URL")
GMAIL_USER     = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
# GOOGLE CALENDAR
# ─────────────────────────────────────────────
def get_calendar_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def extract_actions(response_text):
    actions = []
    for urgency, pattern in [
        ("URGENT", r'\[URGENT\][:\s]+(.+?)(?:\n|$)'),
        ("MEDIUM", r'\[MEDIUM\][:\s]+(.+?)(?:\n|$)'),
        ("LOW",    r'\[LOW\][:\s]+(.+?)(?:\n|$)'),
    ]:
        for a in re.findall(pattern, response_text):
            actions.append({"title": a.strip()[:80], "urgency": urgency, "description": a.strip()})
    if not actions:
        actions.append({
            "title": "Review crop protection plan",
            "urgency": "MEDIUM",
            "description": "Review and apply the crop protection recommendations from your AI advisor."
        })
    return actions[:8]


def smart_schedule(action_text, urgency, now, used_slots):
    text = action_text.lower()
    recurrence = None
    day_offset = None
    hour = None

    # "in X hours" / "within X hours"
    m = re.search(r'(?:within|in|after)\s+(\d+)\s*h(?:our)?', text)
    if m:
        h = int(m.group(1))
        candidate = now + timedelta(hours=h)
        day_offset = (candidate.date() - now.date()).days
        hour = candidate.hour

    # "in X days" / "within X days"
    if day_offset is None:
        m = re.search(r'(?:within|in|after)\s+(\d+)\s*day', text)
        if m:
            day_offset = int(m.group(1))

    # Named day offsets
    if day_offset is None:
        if any(w in text for w in ["immediately", "asap", "right away", "straight away", "urgently", "now", "today"]):
            day_offset = 0
        elif any(w in text for w in ["tomorrow", "next morning", "first thing"]):
            day_offset = 1
        elif any(w in text for w in ["48 hour", "48h", "two day"]):
            day_offset = 2
        elif any(w in text for w in ["72 hour", "72h", "three day"]):
            day_offset = 3
        elif any(w in text for w in ["next week", "7 day", "one week"]):
            day_offset = 7
        elif any(w in text for w in ["10 day", "ten day"]):
            day_offset = 10
        elif any(w in text for w in ["fortnight", "14 day", "two week"]):
            day_offset = 14

    # Urgency fallback
    if day_offset is None:
        if urgency == "URGENT":
            day_offset = 0
        elif urgency == "MEDIUM":
            day_offset = 2
        else:
            day_offset = 5

    # Preferred time of day
    if hour is None:
        if any(w in text for w in ["morning", "early", "sunrise", "first light", "dawn"]):
            hour = 7
        elif any(w in text for w in ["evening", "dusk", "night", "sunset"]):
            hour = 18
        elif any(w in text for w in ["afternoon", "noon", "midday"]):
            hour = 13
        elif urgency == "URGENT":
            hour = 9
        elif urgency == "MEDIUM":
            hour = 10
        else:
            hour = 11

    # Never schedule before submission time
    candidate = now.replace(minute=0, second=0, microsecond=0)
    candidate = candidate.replace(hour=min(hour, 22)) + timedelta(days=day_offset)
    earliest = now + timedelta(minutes=30)
    if candidate < earliest:
        candidate = (earliest + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    day_offset = (candidate.date() - now.date()).days
    hour = candidate.hour

    # Avoid slot collisions — bump to next free hour
    slot_hour = hour
    bump = 0
    while (day_offset, slot_hour) in used_slots:
        slot_hour += 1
        if slot_hour > 21:
            slot_hour = 9
            day_offset += 1
        bump += 1
        if bump > 48:
            break
    used_slots.add((day_offset, slot_hour))

    start = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
    if start < now:
        start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    # Duration by task type
    if any(w in text for w in ["scout", "inspect", "check", "monitor", "survey", "assess", "evaluate"]):
        duration = 1
    elif any(w in text for w in ["spray", "apply", "treat", "fungicide", "pesticide", "herbicide"]):
        duration = 2
    elif any(w in text for w in ["drain", "pump", "clear drain", "remove water", "irrigat"]):
        duration = 2
    elif any(w in text for w in ["harvest", "collect", "pick", "thresh"]):
        duration = 4
    else:
        duration = 1

    # Recurrence
    if any(w in text for w in ["daily", "every day", "each day", "every morning"]):
        recurrence = ["RRULE:FREQ=DAILY;COUNT=21"]
    elif any(w in text for w in ["every week", "each week", "weekly"]):
        recurrence = ["RRULE:FREQ=WEEKLY;COUNT=4"]
    elif any(w in text for w in ["every 2 day", "every other day", "twice a week", "every 48"]):
        recurrence = ["RRULE:FREQ=DAILY;INTERVAL=2;COUNT=14"]
    elif any(w in text for w in ["every 3 day", "every 72"]):
        recurrence = ["RRULE:FREQ=DAILY;INTERVAL=3;COUNT=10"]

    return start, duration, recurrence


def create_calendar_events(actions, location):
    try:
        service = get_calendar_service()
        now = datetime.utcnow()
        created = []
        used_slots = set()
        urgency_emoji = {"URGENT": "🚨", "MEDIUM": "⚡", "LOW": "✅"}

        for action in actions:
            start, duration, recurrence = smart_schedule(
                action["description"], action["urgency"], now, used_slots
            )
            end = start + timedelta(hours=duration)
            event = {
                "summary": f"{urgency_emoji.get(action['urgency'], '🌾')} {action['title']}",
                "location": location,
                "description": (
                    f"Priority: {action['urgency']}\n\n"
                    f"{action['description']}\n\n"
                    f"Generated by AI Crop Protection Advisor"
                ),
                "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Africa/Casablanca"},
                "end":   {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": "Africa/Casablanca"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email", "minutes": 60},
                        {"method": "popup", "minutes": 30}
                    ]
                }
            }
            if recurrence:
                event["recurrence"] = recurrence
            service.events().insert(calendarId="primary", body=event).execute()
            label = action["title"] + (" (recurring)" if recurrence else "")
            created.append(label)
            print(f"✅ Calendar event: {label} @ {start.strftime('%Y-%m-%d %H:%M')}")

        # 21-day daily morning monitoring reminder — skip if one already exists tomorrow
        monitor_start = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        tomorrow_min = monitor_start.strftime("%Y-%m-%dT07:00:00Z")
        tomorrow_max = monitor_start.strftime("%Y-%m-%dT09:00:00Z")
        existing = service.events().list(
            calendarId="primary",
            timeMin=tomorrow_min,
            timeMax=tomorrow_max,
            q="Daily Crop Check"
        ).execute()
        if existing.get("items"):
            print("⚠️ Daily monitoring reminder already exists — skipping duplicate")
            created.append("🌾 Daily Crop Monitoring — already scheduled")
            return created

        monitor_event = {
            "summary": "🌾 Daily Crop Check — Monitor disease, pests & stress",
            "location": location,
            "description": (
                "Daily crop monitoring reminder.\n\n"
                "Check for:\n"
                "- New disease symptoms (spots, mold, rust, lesions)\n"
                "- Pest activity (insects, larvae, chewing damage)\n"
                "- Soil moisture and irrigation status\n"
                "- Plant stress signs (wilting, yellowing, lodging)\n"
                "- Progress of any ongoing treatments\n\n"
                "Generated by AI Crop Protection Advisor"
            ),
            "start": {"dateTime": monitor_start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Africa/Casablanca"},
            "end":   {"dateTime": (monitor_start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Africa/Casablanca"},
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=21"],
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 60},
                    {"method": "popup", "minutes": 15}
                ]
            }
        }
        service.events().insert(calendarId="primary", body=monitor_event).execute()
        created.append("🌾 Daily Crop Monitoring — 21 days recurring")
        print("✅ 21-day daily monitoring reminder created")

        return created
    except Exception as e:
        print(f"❌ Calendar error: {e}")
        return []


# ─────────────────────────────────────────────
# EMAIL  —  100% table-based, no flex/grid
# ─────────────────────────────────────────────
def format_response_as_html(text):

    # Weather
    weather = "N/A"
    for line in text.split("\n"):
        if "🌡️" in line or "WEATHER:" in line:
            weather = line.replace("🌡️", "").replace("WEATHER:", "").strip()
            break

    # Threat level
    threat_level = "MEDIUM"
    threat_color = "#a07a00"
    threat_bg    = "#fff8e6"
    threat_emoji = "⚡"
    for line in text.split("\n"):
        if "THREAT LEVEL" in line or "⚠️" in line:
            up = line.upper()
            if "CRITICAL" in up:
                threat_level, threat_color, threat_bg, threat_emoji = "CRITICAL", "#c00000", "#ffe6e6", "🚨"
            elif "HIGH" in up:
                threat_level, threat_color, threat_bg, threat_emoji = "HIGH", "#c05a00", "#fff0e6", "⚠️"
            elif "LOW" in up:
                threat_level, threat_color, threat_bg, threat_emoji = "LOW", "#2d7a4a", "#e8f5ee", "✅"
            break

    # WHY
    threat_why = ""
    for line in text.split("\n"):
        if "💬" in line or "WHY:" in line:
            threat_why = line.replace("💬", "").replace("WHY:", "").strip()
            break

    # Active threats
    threats, in_threats = [], False
    for line in text.split("\n"):
        if "ACTIVE THREATS" in line or "🔴" in line:
            in_threats = True; continue
        if in_threats:
            if line.strip().startswith("- "):
                threats.append(line.strip()[2:])
            elif line.strip() == "" or any(x in line for x in ["✅","💧","📧","📅","IMMEDIATE","IRRIGATION"]):
                break

    # Immediate actions
    actions, in_actions = [], False
    for line in text.split("\n"):
        if "IMMEDIATE ACTIONS" in line or "✅" in line:
            in_actions = True; continue
        if in_actions:
            if line.strip().startswith("- "):
                actions.append(line.strip()[2:])
            elif line.strip() == "" or any(x in line for x in ["💧","📧","📅","IRRIGATION"]):
                break

    # Irrigation
    irrigation = ""
    for line in text.split("\n"):
        if "💧" in line or "IRRIGATION:" in line:
            irrigation = line.replace("💧","").replace("IRRIGATION:","").strip()
            break

    # Threats HTML
    level_map = {
        "CRITICAL": ("background:#ffe6e6;color:#c00000;", "#c00000"),
        "HIGH":     ("background:#fff0e6;color:#c05a00;", "#c05a00"),
        "MEDIUM":   ("background:#fff8e6;color:#a07a00;", "#a07a00"),
        "LOW":      ("background:#e8f5ee;color:#2d7a4a;", "#2d7a4a"),
    }
    threats_html = ""
    for t in threats[:5]:
        risk_tag = ""; tag_style = ""; left_col = "#e0d8c8"
        for lv, (ts, col) in level_map.items():
            if f"— {lv}" in t.upper() or f"- {lv}" in t.upper():
                risk_tag = lv; tag_style = ts; left_col = col; break

        clean = re.sub(r'\s*[—\-]\s*(CRITICAL|HIGH|MEDIUM|LOW)\s*:?', ':', t, flags=re.IGNORECASE).strip()
        parts  = clean.split(":", 1)
        name   = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else ""

        tag_html = (
            f'<span style="display:inline-block;font-size:11px;font-weight:700;'
            f'padding:2px 8px;border-radius:4px;{tag_style}">{risk_tag}</span>&nbsp;'
        ) if risk_tag else ""

        threats_html += f"""
    <tr>
      <td style="width:4px;background:{left_col};padding:0;line-height:0;">&nbsp;</td>
      <td style="padding:12px 16px;vertical-align:top;border-bottom:1px solid #f0ede6;">
        <div style="margin-bottom:5px;">{tag_html}<strong style="font-size:14px;color:#1a2820;">{name}</strong></div>
        <div style="font-size:13px;color:#5a6e62;line-height:1.7;">{reason}</div>
      </td>
    </tr>"""

    # Actions HTML
    action_map = {
        "URGENT": ("background:#ffe6e6;color:#c00000;", "#c00000"),
        "MEDIUM": ("background:#fff8e6;color:#a07a00;", "#a07a00"),
        "LOW":    ("background:#e8f5ee;color:#2d7a4a;", "#2d7a4a"),
    }
    actions_html = ""
    for a in actions[:5]:
        tag = ""; tag_style = ""; border_col = "#e0d8c8"
        for key, (ts, bc) in action_map.items():
            if f"[{key}]" in a:
                tag = key; tag_style = ts; border_col = bc
                a = a.replace(f"[{key}]", "").strip(); break

        tag_html = (
            f'<span style="display:inline-block;font-size:11px;font-weight:700;'
            f'padding:2px 8px;border-radius:4px;margin-bottom:6px;{tag_style}">{tag}</span>'
        ) if tag else ""

        actions_html += f"""
    <tr>
      <td style="width:4px;background:{border_col};padding:0;line-height:0;">&nbsp;</td>
      <td style="padding:12px 16px;vertical-align:top;border-bottom:1px solid #f0ede6;">
        {tag_html}
        <div style="font-size:14px;color:#1a2820;line-height:1.7;">{a}</div>
      </td>
    </tr>"""

    return (weather, threat_level, threat_color, threat_bg, threat_emoji,
            threat_why, threats_html, actions_html, irrigation)


def send_email(to_email, farmer_name, location, crop, response_text, calendar_events):
    try:
        (weather, threat_level, threat_color, threat_bg, threat_emoji,
         threat_why, threats_html, actions_html, irrigation) = format_response_as_html(response_text)

        cal_rows = ""
        if calendar_events:
            for e in calendar_events:
                cal_rows += f'<tr><td style="padding:4px 0;font-size:13px;color:#1a3a2a;line-height:1.6;">• {e}</td></tr>'
            calendar_section = f"""
      <tr><td colspan="3" style="padding:16px 0 0;">&nbsp;</td></tr>
      <tr>
        <td colspan="3" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="4" bgcolor="#2d7a4a" style="padding:0;line-height:0;">&nbsp;</td>
              <td bgcolor="#e8f5ee" style="padding:14px 18px;border-radius:0 8px 8px 0;">
                <div style="font-size:11px;font-weight:700;color:#1a3a2a;text-transform:uppercase;
                            letter-spacing:1px;margin-bottom:10px;">📅 Calendar Reminders Created</div>
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  {cal_rows}
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>"""
        else:
            calendar_section = ""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🌾 Crop Alert — {threat_level} RISK — {farmer_name} ({crop})"
        msg["From"]    = GMAIL_USER
        msg["To"]      = to_email

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0ede6;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f0ede6">
<tr><td align="center" style="padding:30px 12px;">
  <table width="600" cellpadding="0" cellspacing="0" border="0"
         style="max-width:600px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.12);">
    <!-- HEADER -->
    <tr>
      <td bgcolor="#1a3a2a" style="padding:28px 24px;text-align:center;">
        <div style="font-size:36px;">🌾</div>
        <h1 style="color:#c9a84c;margin:8px 0 4px;font-size:22px;letter-spacing:1px;font-family:Arial,sans-serif;">Crop Protection Alert</h1>
        <p style="color:rgba(255,255,255,0.45);margin:0;font-size:11px;letter-spacing:2px;text-transform:uppercase;">AI Crop Protection Advisor</p>
      </td>
    </tr>
    <!-- THREAT BLOCK -->
    <tr>
      <td bgcolor="{threat_bg}" style="padding:16px 24px;border-bottom:3px solid {threat_color};">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td width="50" style="font-size:30px;text-align:center;vertical-align:middle;">{threat_emoji}</td>
            <td style="padding-left:12px;vertical-align:middle;">
              <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:{threat_color};">Threat Level</div>
              <div style="font-size:19px;font-weight:700;color:{threat_color};margin:3px 0;">{threat_level} RISK</div>
              <div style="font-size:12px;color:#4a5e52;font-style:italic;">{threat_why}</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    <!-- BODY -->
    <tr>
      <td style="padding:24px;background:#ffffff;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <!-- Farmer + Weather -->
          <tr>
            <td width="49%" bgcolor="#f9f6f0" style="padding:12px 14px;border-radius:8px;vertical-align:top;">
              <div style="font-size:10px;font-weight:700;color:#4a5e52;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">👨‍🌾 Farmer</div>
              <div style="font-size:15px;font-weight:700;color:#1a2820;">{farmer_name}</div>
              <div style="font-size:12px;color:#4a5e52;margin-top:3px;">📍 {location} | 🌱 {crop}</div>
            </td>
            <td width="2%">&nbsp;</td>
            <td width="49%" bgcolor="#e8f5ee" style="padding:12px 14px;border-radius:8px;vertical-align:top;">
              <div style="font-size:10px;font-weight:700;color:#4a5e52;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">🌡️ Current Weather</div>
              <div style="font-size:13px;color:#1a2820;line-height:1.6;">{weather}</div>
            </td>
          </tr>
          <tr><td colspan="3" style="height:16px;">&nbsp;</td></tr>
          <!-- ACTIVE THREATS -->
          <tr>
            <td colspan="3" style="padding:0;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td bgcolor="#fff0e6" style="padding:10px 14px;border-radius:8px 8px 0 0;">
                    <span style="font-size:11px;font-weight:700;color:#c05a00;text-transform:uppercase;letter-spacing:1px;">🔴 Active Threats</span>
                  </td>
                </tr>
                {threats_html}
              </table>
            </td>
          </tr>
          <tr><td colspan="3" style="height:16px;">&nbsp;</td></tr>
          <!-- IMMEDIATE ACTIONS -->
          <tr>
            <td colspan="3" style="padding:0;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td bgcolor="#e8f5ee" style="padding:10px 14px;border-radius:8px 8px 0 0;">
                    <span style="font-size:11px;font-weight:700;color:#1a3a2a;text-transform:uppercase;letter-spacing:1px;">✅ Immediate Actions</span>
                  </td>
                </tr>
                {actions_html}
              </table>
            </td>
          </tr>
          <tr><td colspan="3" style="height:16px;">&nbsp;</td></tr>
          <!-- IRRIGATION -->
          <tr>
            <td colspan="3" style="padding:0;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="4" bgcolor="#4a8ccc" style="padding:0;line-height:0;">&nbsp;</td>
                  <td bgcolor="#f0f8ff" style="padding:12px 16px;border-radius:0 8px 8px 0;">
                    <div style="font-size:11px;font-weight:700;color:#1a4a7a;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">💧 Irrigation Advice</div>
                    <div style="font-size:14px;color:#1a2820;line-height:1.7;">{irrigation}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          {calendar_section}
        </table>
      </td>
    </tr>
    <!-- FOOTER -->
    <tr>
      <td bgcolor="#1a3a2a" style="padding:16px 24px;text-align:center;">
        <p style="color:rgba(255,255,255,0.4);font-size:11px;margin:0;letter-spacing:0.5px;">
          Generated automatically by your AI Crop Protection Advisor<br>
          Multi-Agent AI &bull; Real-Time Weather &bull; Agricultural Knowledge Base
        </p>
      </td>
    </tr>
  </table>
</td></tr>
</table>
</body>
</html>"""

        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, to_email, msg.as_string())
        print(f"✅ Email sent to {to_email}")

    except Exception as e:
        print(f"❌ Email error: {e}")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def extract_email(text):
    match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', text)
    return match.group(0) if match else None

def extract_field(text, field):
    patterns = {
        "name":     r"I am ([^,]+),",
        "city":     r"farmer from ([^.]+)\.",
        "location": r"farm is located at: ([^.]+)\.",
        "crop":     r"I grow ([^a]+)",
    }
    match = re.search(patterns.get(field, ""), text)
    return match.group(1).strip() if match else "Unknown"


# ─────────────────────────────────────────────
# MAIN ROUTE
# ─────────────────────────────────────────────
@app.route('/invoke', methods=['POST'])
def invoke():
    data        = request.json
    prompt_text = data.get("input", [{}])[0].get("text", "")

    response = requests.post(API_URL, headers={
        "Content-Type": "application/json",
        "x-api-key": API_KEY
    }, json=data)

    if response.status_code != 200:
        print(f"❌ API Error: {response.status_code} — {response.text}")
        return jsonify({"error": f"API error {response.status_code}"}), response.status_code

    result = response.json()

    response_text = ""
    if isinstance(result.get("output"), str):
        response_text = result["output"]
    elif isinstance(result.get("output"), list):
        for item in result["output"]:
            if isinstance(item, dict) and item.get("type") == "text":
                response_text = item.get("text", "")
                break

    to_email     = extract_email(prompt_text)
    farmer_name  = extract_field(prompt_text, "name")
    city         = extract_field(prompt_text, "city")
    full_address = extract_field(prompt_text, "location")
    location     = full_address if full_address != "Unknown" else city
    crop         = extract_field(prompt_text, "crop")

    # Run email and calendar in parallel — don't block the response
    def run_notifications():
        calendar_events = []
        if os.path.exists("credentials.json"):
            actions = extract_actions(response_text)
            calendar_events = create_calendar_events(actions, location)
        else:
            print("⚠️ credentials.json not found — skipping calendar")
        if to_email:
            send_email(to_email, farmer_name, location, crop, response_text, calendar_events)

    threading.Thread(target=run_notifications, daemon=True).start()

    return jsonify(result), 200


if __name__ == '__main__':
    print("🌾 Server running on http://localhost:5000")
    app.run(port=5000, debug=False)