import json
import requests
import base64
import streamlit as st
import os

TOKEN_PATH = '.streamlit/schwab_tokens.json'

def refresh_access_token():
    """Silently trades the refresh token for a brand new access token."""
    print("🔄 Access token expired. Refreshing quietly in the background...")
    
    with open(TOKEN_PATH, 'r') as f:
        tokens = json.load(f)
        
    # Streamlit securely loads your keys from secrets.toml
    APP_KEY = st.secrets["schwab"]["APP_KEY"]
    APP_SECRET = st.secrets["schwab"]["APP_SECRET"]
    
    headers = {
        'Authorization': f'Basic {base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    payload = {
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token']
    }
    
    response = requests.post('https://api.schwabapi.com/v1/oauth/token', headers=headers, data=payload)
    
    if response.status_code == 200:
        new_tokens = response.json()
        # Schwab sometimes doesn't send a new refresh token, so we keep the old one just in case
        if 'refresh_token' not in new_tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']
            
        with open(TOKEN_PATH, 'w') as f:
            json.dump(new_tokens, f)
            
        return new_tokens['access_token']
    else:
        print("❌ CRITICAL: Failed to refresh token. You may need to run schwab_auth.py again.")
        return None

def fetch_live_quote(symbol="$SPX"):
    """Fetches a live quote, automatically refreshing the token if needed."""
    with open(TOKEN_PATH, 'r') as f:
        tokens = json.load(f)
        
    access_token = tokens['access_token']
    url = "https://api.schwabapi.com/marketdata/v1/quotes"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    response = requests.get(url, headers=headers, params={"symbols": symbol})
    
    # 401 means Unauthorized (Token Expired!)
    if response.status_code == 401:
        new_token = refresh_access_token()
        headers["Authorization"] = f"Bearer {new_token}"
        response = requests.get(url, headers=headers, params={"symbols": symbol})
        
    if response.status_code == 200:
        data = response.json()
        # Extract the exact data points we need for the dashboard metrics
        quote = data[symbol]['quote']
        return {
            "lastPrice": quote['lastPrice'],
            "openPrice": quote['openPrice'],
            "closePrice": quote['closePrice'], # Prior day close
            "netChange": quote['netChange']
        }
    else:
        return None