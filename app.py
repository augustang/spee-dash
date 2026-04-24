import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
import datetime
import pytz
import math
import time
import schwab_client
import os
import json

# --- CLOUD DEPLOYMENT: Fetch latest Schwab tokens from GitHub Gist ---
if not os.path.exists('.streamlit/schwab_tokens.json'):
    os.makedirs('.streamlit', exist_ok=True)
    import gist_sync
    tokens = gist_sync.fetch_tokens_from_gist(use_streamlit=True)
    if tokens:
        with open('.streamlit/schwab_tokens.json', 'w') as f:
            json.dump(tokens, f)
    else:
        st.error("Could not fetch Schwab tokens from Gist. Check your gist secrets.")

# --- INITIALIZE SESSION STATE MEMORY ---
if 'selected_short' not in st.session_state:
    st.session_state.selected_short = None
if 'selected_long' not in st.session_state:
    st.session_state.selected_long = None
if 'selected_spread_px' not in st.session_state:
    st.session_state.selected_spread_px = 0.00
if 'saved_entry' not in st.session_state:
    st.session_state.saved_entry = 0.00
if 'saved_close' not in st.session_state:
    st.session_state.saved_close = 0.00
if 'saved_bp' not in st.session_state:
    st.session_state.saved_bp = 150000
if 'saved_spread' not in st.session_state:
    st.session_state.saved_spread = 10
if 'saved_contracts' not in st.session_state:
    st.session_state.saved_contracts = 150
if 'saved_target' not in st.session_state:
    st.session_state.saved_target = 1500

# --- The Gatekeeper Tracker! ---
if 'last_selected_short' not in st.session_state:
    st.session_state.last_selected_short = None

# 1. Page Setup & Load CSS
st.set_page_config(page_title="SPX Dashboard", layout="wide")

def load_css(file_name):
    try:
        with open(file_name) as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning("Could not find style.css. Make sure it is in the same folder as app.py")

load_css("style.css")

# --- FINANCIAL EVENT DATE HELPERS ---
def _nth_weekday(year, month, weekday, n):
    """Return the nth occurrence of a weekday in a given month (1-indexed).
    weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri"""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))

FOMC_DATES = [
    datetime.date(2025, 1, 29), datetime.date(2025, 3, 19),
    datetime.date(2025, 5, 7),  datetime.date(2025, 6, 18),
    datetime.date(2025, 7, 30), datetime.date(2025, 9, 17),
    datetime.date(2025, 10, 29), datetime.date(2025, 12, 17),
    datetime.date(2026, 1, 28), datetime.date(2026, 3, 18),
    datetime.date(2026, 5, 6),  datetime.date(2026, 6, 17),
    datetime.date(2026, 7, 29), datetime.date(2026, 9, 16),
    datetime.date(2026, 10, 28), datetime.date(2026, 12, 16),
]

def get_financial_events(start_date, end_date):
    """Return sorted list of (datetime, label) for financial events in range."""
    start_d = start_date.date() if hasattr(start_date, 'date') else start_date
    end_d = end_date.date() if hasattr(end_date, 'date') else end_date
    events = []

    current = datetime.date(start_d.year, start_d.month, 1)
    while current <= end_d:
        y, m = current.year, current.month

        opex = _nth_weekday(y, m, 4, 3)
        if start_d <= opex <= end_d:
            events.append((opex, f"{opex.strftime('%b')} OPEX"))

        next_y, next_m = (y, m + 1) if m < 12 else (y + 1, 1)
        vix_exp = _nth_weekday(next_y, next_m, 4, 3) - datetime.timedelta(days=30)
        if start_d <= vix_exp <= end_d:
            events.append((vix_exp, "VIX Exp"))

        if m == 11:
            tday = _nth_weekday(y, 11, 3, 4)
            if start_d <= tday <= end_d:
                events.append((tday, "Thanksgiving"))

        if m == 12:
            xmas = datetime.date(y, 12, 25)
            nye = datetime.date(y, 12, 31)
            if start_d <= xmas <= end_d:
                events.append((xmas, "Xmas"))
            if start_d <= nye <= end_d:
                events.append((nye, "NYE"))

        current = datetime.date(y + (1 if m == 12 else 0), (m % 12) + 1, 1)

    for d in FOMC_DATES:
        if start_d <= d <= end_d:
            events.append((d, "FOMC"))

    events.sort(key=lambda x: x[0])
    return events

# 2. Header & Live Time Logic
eastern = pytz.timezone('America/New_York')
now = datetime.datetime.now(eastern)
date_str = now.strftime("%A %B %-d, %Y")
time_str = now.strftime("%H:%M")

@st.cache_data(ttl=300, show_spinner=False)
def get_market_hours():
    return schwab_client.fetch_market_hours()

market_info = get_market_hours()

if market_info and market_info.get('start') and market_info.get('end'):
    mkt_start = market_info['start']
    mkt_end = market_info['end']
    if now < mkt_start:
        time_diff = mkt_start - now
        hours = int(time_diff.total_seconds() // 3600)
        minutes = int((time_diff.total_seconds() % 3600) // 60)
        status_str = f"{hours}h {minutes}m until open"
    elif now <= mkt_end:
        time_diff = mkt_end - now
        hours = int(time_diff.total_seconds() // 3600)
        minutes = int((time_diff.total_seconds() % 3600) // 60)
        status_str = f"{hours}h {minutes}m until close"
    else:
        status_str = "(Market Closed)"
elif market_info:
    status_str = "(Market Closed)"
else:
    status_str = ""

header_html = f"""
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
        <div style="font-size: 16px; font-weight: 400;">
            <span style="color: black;"><b>{date_str.split(' ')[0]}</b> {' '.join(date_str.split(' ')[1:])}</span>
            <span style="color: #B71AFF; margin-left: 15px; font-weight: 400;">{time_str}</span>
            <span style="color: #D57CFF; font-size: 16px; font-weight: 400;">{status_str}</span>
        </div>
    </div>
"""
st.markdown(header_html, unsafe_allow_html=True)

# 3. Live Schwab Data Fetching Logic
@st.cache_data(ttl=60, show_spinner=False)
def get_spx_metrics():
    try:
        quote_data = schwab_client.fetch_live_quote("$SPX")
        if quote_data:
            spx_last = quote_data['lastPrice']
            spx_open = quote_data['openPrice']
            spx_prior = quote_data['closePrice'] # Schwab gives us yesterday's close for free!
            pts_change = quote_data['netChange']
            pct_change = (pts_change / spx_open) * 100
            
            # Format string for the green pill
            arrow = "↑" if pts_change >= 0 else "↓"
            delta_string = f"{arrow} {abs(pts_change):.2f} pts ({abs(pct_change):.2f}%)"
            return spx_last, spx_open, spx_prior, delta_string
            
    except Exception as e:
        pass 
        
    return 6850.00, 6860.00, 6800.00, "0 pts (0%)" # Fallback if API fails

# 1. Establish the baseline for the whole app (fixes the NameError!)
spx_last, spx_open, spx_prior_close, delta_string = get_spx_metrics()

# 2. Wrap JUST the UI inside the auto-refreshing fragment
@st.fragment(run_every=10)
def render_top_metrics():
    # The fragment fetches its own fresh data quietly in the background
    f_last, f_open, f_prior, f_delta = get_spx_metrics()

# 1. Fetch VIX Data from Schwab
vix_quote = schwab_client.fetch_live_quote("$VIX")
vix9d_quote = schwab_client.fetch_live_quote("$VIX9D") # Sometimes $VX9D depending on the broker mapping, but let's try this!

vix_last = vix_quote['lastPrice'] if vix_quote else 0.00
vix9d_last = vix9d_quote['lastPrice'] if vix9d_quote else 0.00

# --- FETCH SPX HISTORY: INTRADAY (1d/3d/5d) ---
@st.cache_data(ttl=60, show_spinner=False)
def get_spx_history_intraday(period="1d"):
    import time
    import pandas as pd

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (86400 * 1000 * 10)

    history_data = schwab_client.fetch_price_history(
        symbol="$SPX", period_type="day", freq_type="minute", freq=5,
        start_date=start_ms, end_date=now_ms
    )

    if history_data and 'candles' in history_data:
        df = pd.DataFrame(history_data['candles'])
        if df.empty:
            return df
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df['datetime'] = df['datetime'].dt.tz_localize('UTC').dt.tz_convert('America/New_York').dt.tz_localize(None)

        unique_dates = sorted(df['datetime'].dt.date.unique())
        day_map = {"1d": -1, "3d": -3, "5d": -5}
        target_dates = unique_dates[day_map.get(period, -1):]
        df = df[df['datetime'].dt.date.isin(target_dates)]

        df.set_index('datetime', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df

    return pd.DataFrame()


# --- FETCH SPX HISTORY: HISTORICAL (1mo/3mo/6mo) ---
@st.cache_data(ttl=3600, show_spinner=False)
def get_spx_history_historical(period="1mo"):
    import time
    import pandas as pd

    now_ms = int(time.time() * 1000)
    days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "8mo": 240, "12mo": 365}
    days = days_map.get(period, 30)
    start_ms = now_ms - (86400 * 1000 * days)

    history_data = schwab_client.fetch_price_history(
        symbol="$SPX", period_type="year", freq_type="daily", freq=1,
        start_date=start_ms, end_date=now_ms
    )

    if history_data and 'candles' in history_data:
        df = pd.DataFrame(history_data['candles'])
        if df.empty:
            return df
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df['datetime'] = df['datetime'].dt.tz_localize('UTC').dt.tz_convert('America/New_York').dt.tz_localize(None)

        df['datetime'] = df['datetime'].dt.normalize()

        df.set_index('datetime', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df

    return pd.DataFrame()

# --- FETCH LIVE OPTIONS DATA ---
@st.cache_data(ttl=60, show_spinner=False)
def get_spx_puts():
    try:
        chain_data = schwab_client.fetch_options_chain("$SPX")
        
        if not chain_data or 'putExpDateMap' not in chain_data:
            return pd.DataFrame()
            
        put_map = chain_data['putExpDateMap']
        if not put_map:
            return pd.DataFrame()
            
        closest_exp_date = sorted(put_map.keys())[0]
        closest_puts = put_map[closest_exp_date]
        
        put_list = []
        for strike, strike_data in closest_puts.items():
            option = strike_data[0]
            put_list.append({
                'strike': float(strike),
                'lastPrice': option['last'] if option['last'] > 0 else option['mark'],
                'bid': option['bid'],
                'ask': option['ask'],
                'delta': option.get('delta', 0),
            })
            
        df = pd.DataFrame(put_list)
        df = df.sort_values(by='strike', ascending=False).reset_index(drop=True)
        return df
    except Exception as e:
        st.warning(f"Could not load options data: {e}")
        return pd.DataFrame()

# Actually call the function to get the data using our global spx_last variable
_loader = st.empty()
_loader.markdown(
    '<div style="display: flex; align-items: center; gap: 8px; color: #888; font-size: 13px; font-weight: 400; padding: 8px 0; font-family: Inter, sans-serif;">'
    '<div style="width: 14px; height: 14px; border: 2px solid #ddd; border-top: 2px solid #888; '
    'border-radius: 50%; animation: spin 0.8s linear infinite;"></div>'
    'Loading spreads'
    '</div>'
    '<style>@keyframes spin { to { transform: rotate(360deg); } }</style>',
    unsafe_allow_html=True,
)
live_puts_df = get_spx_puts()
_loader.empty()

# 4. Main Layout Columns
col_left, col_right = st.columns([1.3, 2.7], gap="medium")

with col_left:
    st.markdown('<div class="section-label">Metrics</div>', unsafe_allow_html=True)
    with st.container(border=True):
        m1, m2, m3 = st.columns(3)
        m1.metric(label="SPX Prior Close", value=f"{spx_prior_close:,.0f}")
        m2.metric(label="SPX Open", value=f"{spx_open:,.0f}")
        m3.metric(label="SPX Last", value=f"{spx_last:,.0f}")
        
        v1, v2, _ = st.columns(3)
        v1.metric(label="VIX", value=f"{vix_last:,.0f}") 
        v2.metric(label="VIX9D", value=f"{vix9d_last:,.0f}") 
        
        c1, c2, c3 = st.columns(3)

        gap_pts = spx_open - spx_prior_close
        gap_pct = (gap_pts / spx_prior_close) * 100
        with c1:
            st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Overnight</p>', unsafe_allow_html=True)
            bg_g = "#6DF08C" if gap_pts >= 0 else "#FF4646"
            text_g = "#000000" if gap_pts >= 0 else "#FFFFFF"
            arr_g = "↑" if gap_pts >= 0 else "↓"
            st.markdown(f'<div style="background-color: {bg_g}; color: {text_g}; padding: 4px 8px; border-radius: 8px; display: inline-block; font-weight: 400; font-size: 12px; margin-top: 10px;">{arr_g} {abs(gap_pts):.2f} pts ({abs(gap_pct):.2f}%)</div>', unsafe_allow_html=True)

        pts_change = spx_last - spx_open
        pct_change = (pts_change / spx_open) * 100
        with c2:
            st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Since open</p>', unsafe_allow_html=True)
            bg = "#6DF08C" if pts_change >= 0 else "#FF4646"
            text = "#000000" if pts_change >= 0 else "#FFFFFF"
            arr = "↑" if pts_change >= 0 else "↓"
            st.markdown(f'<div style="background-color: {bg}; color: {text}; padding: 4px 8px; border-radius: 8px; display: inline-block; font-weight: 400; font-size: 12px; margin-top: 10px;">{arr} {abs(pts_change):.2f} pts ({abs(pct_change):.2f}%)</div>', unsafe_allow_html=True)

        prior_pts = spx_last - spx_prior_close
        prior_pct = (prior_pts / spx_prior_close) * 100
        with c3:
            st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Prior close</p>', unsafe_allow_html=True)
            bg2 = "#6DF08C" if prior_pts >= 0 else "#FF4646"
            text2 = "#000000" if prior_pts >= 0 else "#FFFFFF"
            arr2 = "↑" if prior_pts >= 0 else "↓"
            st.markdown(f'<div style="background-color: {bg2}; color: {text2}; padding: 4px 8px; border-radius: 8px; display: inline-block; font-weight: 400; font-size: 12px; margin-top: 10px;">{arr2} {abs(prior_pts):.2f} pts ({abs(prior_pct):.2f}%)</div>', unsafe_allow_html=True)

        st.markdown('<div style="margin-top: 20px;"></div>', unsafe_allow_html=True)
        
        # --- THE CALLBACK: Forces the math to run only when inputs are changed ---
        def update_contracts():
            bp = st.session_state.saved_bp
            sw = st.session_state.saved_spread
            st.session_state.saved_contracts = int(bp / (sw * 100))
            st.session_state.saved_target = int(st.session_state.saved_contracts * 0.10 * 100)

        in1, in2 = st.columns(2)
        # Added on_change callbacks, and removed 'value' so they rely on memory!
        buying_power = in1.number_input("Buying power ($)", step=10000, format="%d", key="saved_bp", on_change=update_contracts)
        spread_width = in2.selectbox("Spread width", [10, 25, 50, 100], key="saved_spread", on_change=update_contracts)
        
        in3, in4 = st.columns(2)
        # Removed 'value' so it stops fighting the memory!
        contracts = in3.number_input("Contracts", step=1, key="saved_contracts")
        target_profit = in4.number_input("Target profit ($)", step=100, key="saved_target")

    st.write("")
    st.markdown('<div class="section-label">Spreads</div>', unsafe_allow_html=True)
    with st.container(border=True):
        spreads_list = []
        seen_strikes = set()
        
        # 1. Generate our exact target percentages: 0.5% to 8.0% in 0.1% ish steps
        target_pcts = [x / 10.0 for x in range(5, 81)] 
        
        # 2. Loop through our targets and find the closest real options
        if not live_puts_df.empty:
            for pct in target_pcts:
                target_price = spx_last * (1 - (pct / 100))
                
                # Snap to the closest real strike to our theoretical target price
                closest_idx = (live_puts_df['strike'] - target_price).abs().idxmin()
                short_put = live_puts_df.loc[closest_idx]
                short_strike = short_put['strike']
                
                # Skip if we already mapped this strike (prevents duplicates from rounding)
                if short_strike in seen_strikes:
                    continue
                seen_strikes.add(short_strike)
                
                long_strike = short_strike - spread_width
                long_put_match = live_puts_df[live_puts_df['strike'] == long_strike]
                
                if not long_put_match.empty:
                    long_put = long_put_match.iloc[0]
                    short_px = short_put['lastPrice']
                    long_px = long_put['lastPrice']
                    spread_price = short_px - long_px
                    
                    if spread_price > 0:
                        pts_out = abs(short_strike - spx_last)
                        actual_pct_out = (pts_out / spx_last) * 100
                        total_premium = spread_price * contracts * 100
                        
                        spreads_list.append({
                            "Pts": int(pts_out),
                            "(%)": f"{actual_pct_out:.1f}%",
                            "Strike": int(short_strike),
                            "Leg": int(long_strike),
                            "Short PX": short_px,
                            "Long PX": long_px,
                            "Spread": spread_price,
                            "Premiums": total_premium
                        })
                        
        df_spreads = pd.DataFrame(spreads_list)
        
        if not df_spreads.empty:
            def highlight_target(row):
                if row['Premiums'] >= target_profit: 
                    return ['background-color: #E4FF7A; color: black; font-weight: bold'] * len(row)
                return [''] * len(row)
                
            styled_df = df_spreads.style.format({
                "Short PX": "{:.2f}",
                "Long PX": "{:.2f}",
                "Spread": "{:.2f}",
                "Premiums": "${:,.0f}" 
            }).apply(highlight_target, axis=1)
            
            selection_event = st.dataframe(
                styled_df, 
                hide_index=True, 
                use_container_width=True,
                height=350, 
                on_select="rerun",
                selection_mode="single-row"
            )
        else:
            st.info("No spreads available — options data may be unavailable outside market hours.")
            selection_event = None

        if selection_event is not None and len(selection_event.selection.rows) > 0:
            selected_idx = selection_event.selection.rows[0]
            current_short = df_spreads.iloc[selected_idx]['Strike']
            
            if current_short != st.session_state.last_selected_short:
                st.session_state.selected_short = current_short
                st.session_state.selected_long = df_spreads.iloc[selected_idx]['Leg']
                st.session_state.saved_entry = float(df_spreads.iloc[selected_idx]['Spread'])
                st.session_state.saved_close = 0.05
                st.session_state.last_selected_short = current_short

        if st.session_state.selected_short is not None and not df_spreads.empty:
            match = df_spreads[df_spreads['Strike'] == st.session_state.selected_short]
            if not match.empty:
                st.session_state.selected_spread_px = float(match.iloc[0]['Spread'])

        selected_short = st.session_state.selected_short
        selected_long = st.session_state.selected_long
        selected_spread_px = st.session_state.selected_spread_px

    # --- CURRENT TRADE SECTION ---
    st.write("") 
    st.markdown('<div class="section-label">Current trade</div>', unsafe_allow_html=True)
    
    with st.container(border=True):
        col1, col2, col3, col4 = st.columns(4)
        
        col1.number_input("Entry PX", step=0.05, min_value=0.00,
                          format="%.2f", key="saved_entry")
        
        col2.number_input("Current PX", value=float(selected_spread_px), disabled=True)
        
        realistic_close = col3.number_input(
            "Realistic Close", step=0.05, min_value=0.00,
            format="%.2f", key="saved_close"
        )
        
        entry_px = st.session_state.saved_entry
        realistic_pl = (entry_px - realistic_close) * contracts * 100
        pl_string = f"+${realistic_pl:,.0f}" if realistic_pl >= 0 else f"-${abs(realistic_pl):,.0f}"
        col4.text_input("Realistic P/L", value=pl_string, disabled=True)

    # --- PROBABILITY OTM SECTION ---
    st.write("")
    st.markdown('<div class="section-label">Probability OTM</div>', unsafe_allow_html=True)

    with st.container(border=True):
        short_prob = "—"
        long_prob = "—"
        if selected_short is not None and not live_puts_df.empty and 'delta' in live_puts_df.columns:
            short_match = live_puts_df[live_puts_df['strike'] == float(selected_short)]
            if not short_match.empty:
                short_prob = f"{(1 - abs(short_match.iloc[0]['delta'])) * 100:.1f}%"
        if selected_long is not None and not live_puts_df.empty and 'delta' in live_puts_df.columns:
            long_match = live_puts_df[live_puts_df['strike'] == float(selected_long)]
            if not long_match.empty:
                long_prob = f"{(1 - abs(long_match.iloc[0]['delta'])) * 100:.1f}%"

        def _prob_field(label, value):
            return f'''<div>
                <p style="font-size:12px;color:#000;margin-bottom:4px;">{label}</p>
                <div style="background:#F1F2F6;border-radius:8px;padding:8px 12px;font-size:12px;color:#000;">{value}</div>
            </div>
            <div style="margin-bottom:12px;"></div>'''
        p1, p2, p3, p4 = st.columns(4)
        strike_val = f"{int(selected_short)}" if selected_short else "—"
        leg_val = f"{int(selected_long)}" if selected_long else "—"
        p1.markdown(_prob_field("Strike", strike_val), unsafe_allow_html=True)
        p2.markdown(_prob_field("Probability", short_prob), unsafe_allow_html=True)
        p3.markdown(_prob_field("Leg", leg_val), unsafe_allow_html=True)
        p4.markdown(_prob_field("Probability", long_prob), unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="section-label">Charts</div>', unsafe_allow_html=True)

    # 2. THE CHART DRAWING ENGINE
    def create_spx_chart(title, prices, dates, line_color, halo_color, events=None, chart_height=420, view_range=None, ohlc_df=None):
        fig = go.Figure()
        
        if ohlc_df is not None:
            hover_texts = [
                f"{d.strftime('%b %-d, %Y')}<br>Open: {o:,.2f}<br>High: {h:,.2f}<br>Low: {l:,.2f}<br>Close: {c:,.2f}"
                for d, o, h, l, c in zip(ohlc_df.index, ohlc_df['Open'], ohlc_df['High'], ohlc_df['Low'], ohlc_df['Close'])
            ]
            fig.add_trace(go.Candlestick(
                x=ohlc_df.index,
                open=ohlc_df['Open'], high=ohlc_df['High'],
                low=ohlc_df['Low'], close=ohlc_df['Close'],
                increasing=dict(line=dict(color="#11F185", width=1), fillcolor="#11F185"),
                decreasing=dict(line=dict(color="#FF3D54", width=1), fillcolor="#FF3D54"),
                line=dict(width=1),
                showlegend=False,
                text=hover_texts,
                hoverinfo="text",
            ))
        else:
            fig.add_trace(go.Scatter(
                x=dates, y=prices, mode='lines', 
                line=dict(color=line_color, width=2),
                showlegend=False
            ))
            if len(dates) > 0 and len(prices) > 0:
                last_date = dates[-1]
                last_price = prices.iloc[-1]
                fig.add_trace(go.Scatter(
                    x=[last_date], y=[last_price], mode='markers',
                    marker=dict(
                        color=line_color, 
                        size=4, 
                        line=dict(color=halo_color, width=8) 
                    ),
                    showlegend=False,
                    hoverinfo='skip'
                ))
            
        # Calculate breathing room (extend to cover upcoming events if present)
        y_range = None
        if len(dates) > 0:
            min_date = dates.min()
            max_date = dates.max()
            date_range = max_date - min_date
            padded_max_date = max_date + (date_range * 0.05)
            if events:
                last_evt = pd.Timestamp(max(e[0] for e in events))
                if last_evt > padded_max_date:
                    padded_max_date = last_evt + (date_range * 0.02)
            view_left = view_range if view_range is not None else min_date
            if ohlc_df is not None:
                vis_df = ohlc_df[(ohlc_df.index >= view_left) & (ohlc_df.index <= padded_max_date)]
                if len(vis_df) > 0:
                    y_min, y_max = vis_df['Low'].min(), vis_df['High'].max()
                else:
                    y_min, y_max = prices.min(), prices.max()
            else:
                visible = prices[(dates >= view_left) & (dates <= padded_max_date)]
                if len(visible) > 0:
                    y_min, y_max = visible.min(), visible.max()
                else:
                    y_min, y_max = prices.min(), prices.max()
            if selected_short is not None:
                y_min = min(y_min, float(selected_long or selected_short))
                y_max = max(y_max, float(selected_short))
            y_pad = (y_max - y_min) * 0.05
            y_range = [y_min - y_pad, y_max + y_pad]
        else:
            min_date, padded_max_date = None, None
            
        # Interactive strike lines
        if selected_short is not None and selected_long is not None:
            fig.add_hline(
                y=selected_short, line_dash="solid", line_color="#4B7BFF", 
                annotation_text=f"Short strike ({selected_short})", 
                annotation_position="top left",
                annotation=dict(font_size=8, font_color="white", bgcolor="#4B7BFF", borderpad=3, bordercolor="#4B7BFF")
            )
            fig.add_hline(
                y=selected_long, line_dash="solid", line_color="#FF6347", 
                annotation_text=f"Long strike ({selected_long})", 
                annotation_position="bottom left",
                annotation=dict(font_size=8, font_color="white", bgcolor="#FF6347", borderpad=3, bordercolor="#FF6347")
            )
            
        if events:
            from collections import defaultdict
            grouped = defaultdict(list)
            for evt_date, evt_label in events:
                grouped[evt_date].append(evt_label)
            for evt_date, labels in grouped.items():
                evt_dt = datetime.datetime.combine(evt_date, datetime.time()) if isinstance(evt_date, datetime.date) else evt_date
                if min_date is not None and pd.Timestamp(min_date) <= pd.Timestamp(evt_dt) <= pd.Timestamp(padded_max_date):
                    combined = ", ".join(labels)
                    fig.add_shape(
                        type="line", x0=evt_dt, x1=evt_dt, y0=0, y1=1,
                        yref="paper", line=dict(dash="1px,3px", color="#B2B2B2", width=1),
                    )
                    fig.add_annotation(
                        x=evt_dt, y=1.01, yref="paper", text=combined,
                        textangle=-90, font=dict(size=7, color="#888888"),
                        showarrow=False, yanchor="bottom", xanchor="center",
                    )

        # Layout rules
        fig.update_layout(
            dragmode="zoom",
            uirevision="constant", 
            height=chart_height, 
            margin=dict(l=60, r=20, t=70 if events else 10, b=30), 
            plot_bgcolor="white", paper_bgcolor="white",
            hovermode="x" if ohlc_df is not None else "x unified",
            hoverdistance=100 if ohlc_df is not None else -1,
            spikedistance=100 if ohlc_df is not None else -1,
            hoverlabel=dict(
                bgcolor="rgba(255, 255, 255, 0.85)", 
                bordercolor="rgba(0, 0, 0, 0)",
                font=dict(color="#1E1E1E")
            ),
            xaxis=dict(
                showgrid=True, gridcolor="#F0F0F0",
                range=[view_range if view_range is not None else min_date, padded_max_date] if min_date else None,
                rangeslider=dict(visible=False),
                showspikes=True, spikemode="across",
                spikesnap="data" if ohlc_df is not None else "cursor",
                spikedash="1, 3", spikecolor="#B2B2B2", spikethickness=1
            ),
            yaxis=dict(
                automargin=False, 
                showgrid=True, gridcolor="#F0F0F0", side="left",
                range=y_range,
                showspikes=True, spikemode="across", spikesnap="cursor", spikedash="1, 3",     
                spikecolor="#B2B2B2", spikethickness=1
            )
        )
        return fig

    # 3. DAY CHART FRAGMENT
    @st.fragment(run_every=60)
    def render_day_chart():
        # We define the variables safely inside the soundproof room!
        day_params = {"1 Day": "1d", "3 Days": "3d", "5 Days": "5d"}
        
        with st.container(border=True):
            # This puts the button inside the box and collapses the title text!
            selected_option = st.radio(
                "Intraday Timeframe", 
                list(day_params.keys()), 
                horizontal=True, 
                key="day_radio",
                label_visibility="collapsed" 
            )
            
            df_day = get_spx_history_intraday(period=day_params[selected_option])
            f_last, f_open, f_prior, f_delta = get_spx_metrics()
            
            is_spx_down = (f_last - f_open) < 0
            spx_theme_color = "#FF3D54" if is_spx_down else "#11F185" 
            spx_halo_color = 'rgba(255, 61, 84, 0.3)' if is_spx_down else 'rgba(17, 241, 133, 0.3)'
            
            st.plotly_chart(
                create_spx_chart(selected_option, df_day['Close'], df_day.index, spx_theme_color, spx_halo_color), 
                use_container_width=True,
                key="day_spx_chart",
                config={'displayModeBar': False} 
            )

    # DRAW DAY CHART
    render_day_chart()

    # 4. MONTH CHART FRAGMENT
    @st.fragment(run_every=120)
    def render_month_chart():
        month_params = {"12 Months": "12mo", "8 Months": "8mo", "6 Months": "6mo", "3 Months": "3mo", "1 Month": "1mo"}
        
        with st.container(border=True):
            radio_col, ev_col, line_col = st.columns([3, 0.5, 0.5])
            with radio_col:
                selected_option = st.radio(
                    "Historical Timeframe", 
                    list(month_params.keys()), 
                    index=2,
                    horizontal=True, 
                    key="month_radio",
                    label_visibility="collapsed"
                )
            with ev_col:
                show_events = st.checkbox("Events", key="show_events")
            with line_col:
                show_line = st.checkbox("Line", key="show_line")
            
            df_month = get_spx_history_historical(period="12mo")
            f_last, f_open, f_prior, f_delta = get_spx_metrics()

            if not df_month.empty:
                now_ts = pd.Timestamp.now('America/New_York').tz_localize(None).normalize()
                if now_ts not in df_month.index:
                    live_row = pd.DataFrame(
                        {'Open': f_last, 'High': f_last, 'Low': f_last, 'Close': f_last},
                        index=[now_ts]
                    )
                    df_month = pd.concat([df_month, live_row])
                else:
                    df_month.loc[now_ts, 'Close'] = f_last
            
            is_spx_down = (f_last - f_open) < 0
            spx_theme_color = "#FF3D54" if is_spx_down else "#11F185" 
            spx_halo_color = 'rgba(255, 61, 84, 0.3)' if is_spx_down else 'rgba(17, 241, 133, 0.3)'
            
            days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "8mo": 240, "12mo": 365}
            view_days = days_map[month_params[selected_option]]
            view_start = now_ts - pd.Timedelta(days=view_days)
            
            events = None
            if show_events and not df_month.empty:
                lookahead = df_month.index.max() + pd.DateOffset(months=1)
                events = get_financial_events(df_month.index.min(), lookahead)
            
            candle_data = None if show_line else df_month
            st.plotly_chart(
                create_spx_chart(selected_option, df_month['Close'], df_month.index, spx_theme_color, spx_halo_color, events=events, chart_height=500, view_range=view_start, ohlc_df=candle_data), 
                use_container_width=True,
                key="month_spx_chart",
                config={'displayModeBar': False} 
            )

    # DRAW MONTH CHART
    render_month_chart()