import os
import re
import smtplib
import requests
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

API_KEY = os.getenv("API_KEY")
API_URL = os.getenv("API_URL")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

app = Flask(__name__)
CORS(app)


# GOOGLE CALENDAR

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
    urgent = re.findall(r'\[URGENT\][:\s]+(.+?)(?:\n|$)', response_text)
    for a in urgent:
        actions.append({"title": a.strip()[:80], "urgency": "URGENT", "description": a.strip()})
    medium = re.findall(r'\[MEDIUM\][:\s]+(.+?)(?:\n|$)', response_text)
    for a in medium:
        actions.append({"title": a.strip()[:80], "urgency": "MEDIUM", "description": a.strip()})
    low = re.findall(r'\[LOW\][:\s]+(.+?)(?:\n|$)', response_text)
    for a in low:
        actions.append({"title": a.strip()[:80], "urgency": "LOW", "description": a.strip()})
    if not actions:
        actions.append({
            "title": "Review crop protection plan",
            "urgency": "MEDIUM",
            "description": "Review and apply the crop protection recommendations from your AI advisor."
        })
    return actions[:6]


def create_calendar_events(actions, location):
    try:
        service = get_calendar_service()
        now = datetime.utcnow()
        created = []
        for action in actions:
            if action["urgency"] == "URGENT":
                start = now + timedelta(hours=1)
            elif action["urgency"] == "MEDIUM":
                start = now + timedelta(days=2)
            else:
                start = now + timedelta(days=6)
            end = start + timedelta(hours=1)
            event = {
                "summary": f"🌾 {action['title']}",
                "location": location,
                "description": action["description"],
                "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Africa/Casablanca"},
                "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Africa/Casablanca"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email", "minutes": 60},
                        {"method": "popup", "minutes": 30}
                    ]
                }
            }
            service.events().insert(calendarId="primary", body=event).execute()
            created.append(action["title"])
            print(f"✅ Calendar event created: {action['title']}")
        return created
    except Exception as e:
        print(f"❌ Calendar error: {e}")
        return []



# EMAIL

def format_response_as_html(text):
    lines = text.split('\n')
    html = ""
    in_list = False
    for line in lines:
        line = line.strip()
        if not line:
            if in_list:
                html += "</ul>"
                in_list = False
            html += "<br>"
            continue
        if line.endswith(':') and len(line) < 60:
            if in_list:
                html += "</ul>"
                in_list = False
            html += f'<h3 style="color:#1a3a2a; margin:20px 0 8px; border-bottom:1px solid #e0d8c8; padding-bottom:6px;">{line}</h3>'
        elif line.startswith(('- ', '* ', '• ')):
            if not in_list:
                html += '<ul style="margin:8px 0; padding-left:20px;">'
                in_list = True
            content = line[2:]
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            content = content.replace('[URGENT]', '<span style="background:#ffe6e6;color:#c00000;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">URGENT</span>')
            content = content.replace('[MEDIUM]', '<span style="background:#fff8e6;color:#a07a00;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">MEDIUM</span>')
            content = content.replace('[LOW]', '<span style="background:#e8f5ee;color:#2d7a4a;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">LOW</span>')
            html += f'<li style="margin:6px 0;line-height:1.6;">{content}</li>'
        elif re.match(r'^\d+\.', line):
            if in_list:
                html += "</ul>"
                in_list = False
            content = re.sub(r'^\d+\.\s*', '', line)
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            content = content.replace('[URGENT]', '<span style="background:#ffe6e6;color:#c00000;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">URGENT</span>')
            content = content.replace('[MEDIUM]', '<span style="background:#fff8e6;color:#a07a00;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">MEDIUM</span>')
            content = content.replace('[LOW]', '<span style="background:#e8f5ee;color:#2d7a4a;padding:2px 6px;border-radius:4px;font-size:12px;font-weight:bold;">LOW</span>')
            html += f'<p style="margin:6px 0;line-height:1.6;">• {content}</p>'
        else:
            if in_list:
                html += "</ul>"
                in_list = False
            line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
            html += f'<p style="margin:6px 0;line-height:1.6;">{line}</p>'
    if in_list:
        html += "</ul>"
    return html


def detect_threat_level(text):
    lower = text.lower()
    if 'critical' in lower or 'emergency' in lower:
        return ('🚨 CRITICAL RISK', '#c00000', '#ffe6e6')
    elif 'high risk' in lower or 'urgent' in lower:
        return ('⚠️ HIGH RISK', '#c05a00', '#fff0e6')
    elif 'medium' in lower or 'moderate' in lower:
        return ('⚡ MEDIUM RISK', '#a07a00', '#fff8e6')
    else:
        return ('✅ LOW RISK', '#2d7a4a', '#e8f5ee')


def send_email(to_email, farmer_name, location, crop, response_text, calendar_events):
    try:
        threat_label, threat_color, threat_bg = detect_threat_level(response_text)
        formatted_body = format_response_as_html(response_text)

        # Build calendar events section
        calendar_html = ""
        if calendar_events:
            calendar_html = """
            <div style="background:#e8f5ee; border-left:4px solid #2d7a4a; padding:16px; margin:20px 0; border-radius:0 8px 8px 0;">
              <p style="margin:0 0 8px; font-weight:bold; color:#1a3a2a;">📅 Calendar Reminders Created:</p>
              <ul style="margin:0; padding-left:20px;">
            """
            for event in calendar_events:
                calendar_html += f'<li style="margin:4px 0; color:#2d5a3d;">{event}</li>'
            calendar_html += "</ul></div>"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🌾 Crop Protection Alert — {threat_label} — {farmer_name}"
        msg["From"] = GMAIL_USER
        msg["To"] = to_email

        html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0ede6;font-family:Arial,sans-serif;">
  <div style="max-width:680px;margin:30px auto;border-radius:16px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.12);">
    <div style="background:linear-gradient(135deg,#1a3a2a,#2d5a3d);padding:32px 24px;text-align:center;">
      <div style="font-size:40px;margin-bottom:8px;">🌾</div>
      <h1 style="color:#c9a84c;margin:0;font-size:26px;letter-spacing:1px;">Crop Protection Alert</h1>
      <p style="color:rgba(255,255,255,0.6);margin:8px 0 0;font-size:13px;letter-spacing:2px;text-transform:uppercase;">AI Crop Protection Advisor</p>
    </div>
    <div style="background:{threat_bg};padding:16px 24px;text-align:center;border-bottom:2px solid {threat_color};">
      <span style="color:{threat_color};font-weight:bold;font-size:16px;letter-spacing:1px;">{threat_label}</span>
    </div>
    <div style="background:#f9f6f0;padding:20px 24px;border-bottom:1px solid #e0d8c8;">
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="padding:6px 12px;color:#4a5e52;font-size:13px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;width:30%;">👨‍🌾 Farmer</td>
          <td style="padding:6px 12px;color:#1a2820;font-size:15px;">{farmer_name}</td>
        </tr>
        <tr>
          <td style="padding:6px 12px;color:#4a5e52;font-size:13px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;">📍 Location</td>
          <td style="padding:6px 12px;color:#1a2820;font-size:15px;">{location}</td>
        </tr>
        <tr>
          <td style="padding:6px 12px;color:#4a5e52;font-size:13px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;">🌱 Crop</td>
          <td style="padding:6px 12px;color:#1a2820;font-size:15px;">{crop}</td>
        </tr>
      </table>
    </div>
    <div style="background:white;padding:28px;color:#1a2820;font-size:15px;line-height:1.8;">
      {formatted_body}
      {calendar_html}
    </div>
    <div style="background:linear-gradient(135deg,#1a3a2a,#2d5a3d);padding:20px 24px;text-align:center;">
      <p style="color:rgba(255,255,255,0.5);font-size:12px;margin:0;letter-spacing:0.5px;">
        Generated automatically by your AI Crop Protection Advisor<br>
        Powered by Multi-Agent AI • Real-Time Weather • Agricultural Knowledge Base
      </p>
    </div>
  </div>
</body>
</html>
        """

        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, to_email, msg.as_string())
        print(f"✅ Email sent to {to_email}")

    except Exception as e:
        print(f"❌ Email error: {e}")



# HELPERS
def extract_email(text):
    match = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]+', text)
    return match.group(0) if match else None

def extract_field(text, field):
    patterns = {
        "name": r"I am ([^,]+),",
        "location": r"from ([^.]+)\.",
        "crop": r"I grow ([^a]+)",
    }
    match = re.search(patterns.get(field, ""), text)
    return match.group(1).strip() if match else "Unknown"



# MAIN ROUTE
@app.route('/invoke', methods=['POST'])
def invoke():
    data = request.json
    prompt_text = data.get("input", [{}])[0].get("text", "")

    response = requests.post(API_URL, headers={
        "Content-Type": "application/json",
        "x-api-key": API_KEY
    }, json=data)

    if response.status_code != 200:
        print(f"❌ API Error: {response.status_code} — {response.text}")
        return jsonify({"error": f"API error {response.status_code}"}), response.status_code

    result = response.json()

    # Extract agent response text
    response_text = ""
    if isinstance(result.get("output"), str):
        response_text = result["output"]
    elif isinstance(result.get("output"), list):
        for item in result["output"]:
            if isinstance(item, dict) and item.get("type") == "text":
                response_text = item.get("text", "")
                break

    to_email = extract_email(prompt_text)
    farmer_name = extract_field(prompt_text, "name")
    location = extract_field(prompt_text, "location")
    crop = extract_field(prompt_text, "crop")

    # ✅ Create calendar events
    calendar_events = []
    if os.path.exists("credentials.json"):
        actions = extract_actions(response_text)
        calendar_events = create_calendar_events(actions, location)
    else:
        print("⚠️ credentials.json not found — skipping calendar")

    # ✅ Send email
    if to_email:
        send_email(to_email, farmer_name, location, crop, response_text, calendar_events)

    return jsonify(result), 200


if __name__ == '__main__':
    print("🌾 Server running on http://localhost:5000")
    app.run(port=5000, debug=True)