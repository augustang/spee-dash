import base64
import requests
import urllib.parse
import json

# --- Read Secrets Manually ---
with open(".streamlit/secrets.toml", "r") as f:
    lines = f.readlines()
    
APP_KEY = [l.split('=')[1].strip().strip('"') for l in lines if "APP_KEY" in l][0]
APP_SECRET = [l.split('=')[1].strip().strip('"') for l in lines if "APP_SECRET" in l][0]
CALLBACK_URL = [l.split('=')[1].strip().strip('"') for l in lines if "CALLBACK_URL" in l][0]

# --- Step 1: Generate Login Link ---
auth_url = f"https://api.schwabapi.com/v1/oauth/authorize?client_id={APP_KEY}&redirect_uri={CALLBACK_URL}"

print("\n=== STEP 1: LOG IN TO SCHWAB ===")
print(f"Click this link to log in: \n{auth_url}\n")
print("After you log in and approve access, your browser will redirect to a page that says 'This site can't be reached'. THAT IS NORMAL!")
print("Look at the URL bar at the top of your browser. It should look like: https://127.0.0.1/?code=xxxxx...")

# --- Step 2: Extract Code ---
redirected_url = input("\n=== STEP 2: PASTE THE ENTIRE REDIRECTED URL HERE ===\n> ")

parsed_url = urllib.parse.urlparse(redirected_url)
code = urllib.parse.parse_qs(parsed_url.query)['code'][0]

# Schwab requires the authorization code to end in an '@' symbol
if not code.endswith('@'):
    code += '@'

# --- Step 3: Trade Code for Tokens ---
print("\n=== STEP 3: FETCHING TOKENS... ===")
headers = {
    'Authorization': f'Basic {base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()}',
    'Content-Type': 'application/x-www-form-urlencoded'
}
payload = {
    'grant_type': 'authorization_code',
    'code': code,
    'redirect_uri': CALLBACK_URL
}

response = requests.post('https://api.schwabapi.com/v1/oauth/token', headers=headers, data=payload)

if response.status_code == 200:
    tokens = response.json()
    with open('.streamlit/schwab_tokens.json', 'w') as f:
        json.dump(tokens, f)
    print("\nSUCCESS! Your tokens are saved securely to .streamlit/schwab_tokens.json")
else:
    print("\nFAILED.")
    print(response.text)