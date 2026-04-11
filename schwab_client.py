import json
import requests
import base64
import streamlit as st
import os

import gist_sync

TOKEN_PATH = '.streamlit/schwab_tokens.json'

def refresh_access_token():
    """Silently trades the refresh token for a brand new access token."""
    print("🔄 Access token expired. Refreshing quietly in the background...")
    
    with open(TOKEN_PATH, 'r') as f:
        tokens = json.load(f)
        
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
        if 'refresh_token' not in new_tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']
            
        with open(TOKEN_PATH, 'w') as f:
            json.dump(new_tokens, f)

        gist_sync.push_tokens_to_gist(new_tokens, use_streamlit=True)
            
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
    
    if response.status_code == 401:
        new_token = refresh_access_token()
        if new_token is None:
            return None
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
    
def fetch_price_history(symbol="$SPX", period_type="day", period=1, freq_type="minute", freq=5, start_date=None, end_date=None):
    """Fetches intraday or historical candles from Schwab using explicit timestamps."""
    import json
    import requests
    
    # Note: Make sure this token path matches what you currently use in this file!
    with open('.streamlit/schwab_tokens.json', 'r') as f:
        tokens = json.load(f)
        
    access_token = tokens['access_token']
    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    params = {
        "symbol": symbol,
        "periodType": period_type,
        "frequencyType": freq_type,
        "frequency": freq
    }
    
    # THE UPGRADE: Override period if we have exact dates
    if start_date and end_date:
        params["startDate"] = int(start_date)
        params["endDate"] = int(end_date)
    else:
        params["period"] = period
        
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 401:
        new_token = refresh_access_token()
        if new_token is None:
            return None
        headers["Authorization"] = f"Bearer {new_token}"
        response = requests.get(url, headers=headers, params=params)
        
    if response.status_code == 200:
        return response.json()
    return None

def fetch_options_chain(symbol="$SPX"):
    """Fetches the near-term Out-Of-The-Money puts."""
    with open(TOKEN_PATH, 'r') as f:
        tokens = json.load(f)
        
    access_token = tokens['access_token']
    url = "https://api.schwabapi.com/marketdata/v1/chains"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    params = {
        "symbol": symbol,
        "contractType": "PUT",
        "includeQuotes": "TRUE",
        "range": "OTM",
        "strikeCount": 150,
        "daysToExpiration": 5
    }
    
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 401:
        new_token = refresh_access_token()
        if new_token is None:
            return None
        headers["Authorization"] = f"Bearer {new_token}"
        response = requests.get(url, headers=headers, params=params)
        
    if response.status_code == 200:
        return response.json()
    return None