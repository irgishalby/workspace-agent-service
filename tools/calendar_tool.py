import os
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service():
    """Handles Google Calendar OAuth2 authentication state."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds)

def create_calendar_event(summary: str, start_time: str, end_time: str = None, description: str = "") -> dict:
    """
    Creates an event in Google Calendar.
    start_time/end_time format: 'YYYY-MM-DDTHH:MM:SS' (ISO 8601 string)
    """
    try:
        service = get_calendar_service()
        
        # Default end time to 1 hour after start if missing
        if not end_time:
            start_dt = datetime.datetime.fromisoformat(start_time)
            end_dt = start_dt + datetime.timedelta(hours=1)
            end_time = end_dt.isoformat()

        event = {
            'summary': summary,
            'description': description,
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Jakarta'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Jakarta'},
        }

        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return {
            "status": "success",
            "summary": summary,
            "htmlLink": created_event.get('htmlLink')
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}