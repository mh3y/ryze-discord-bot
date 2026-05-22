"""One-time script to get a Google OAuth2 refresh token."""
import os
import sys

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_FILE = r"C:\Users\micha\Downloads\client_secret_229357366407-v9hpoqib5eh9jhklktiqedf679uhb0t6.apps.googleusercontent.com.json"

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)

print("\n" + "="*60)
print("Google OAuth2 — Refresh Token Setup")
print("="*60)
print("\nA browser window will open. If it doesn't, copy the URL")
print("printed below and open it in Opera GX manually.\n")

creds = flow.run_local_server(port=8080, open_browser=True)

print("\n" + "="*60)
print("SUCCESS — add this to your .env file:")
print("="*60)
print(f"\nGOOGLE_REFRESH_TOKEN={creds.refresh_token}\n")
