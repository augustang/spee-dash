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

# 2. Header & Live Time Logic
eastern = pytz.timezone('US/Eastern')
now = datetime.datetime.now(eastern)
date_str = now.strftime("%A %B %-d, %Y")
time_str = now.strftime("%H:%M")

market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

if now < market_open:
    status_str = "(Market Pre-Open)"
elif now > market_close:
    status_str = "(Market Closed)"
else:
    time_diff = market_close - now
    hours = int(time_diff.total_seconds() // 3600)
    minutes = int((time_diff.total_seconds() % 3600) // 60)
    status_str = f"{hours}h {minutes}m until close"

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
@st.cache_data(ttl=60) # Refreshes metrics every minute
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

# --- FETCH SPX INTRADAY HISTORY (For the Chart) ---
@st.cache_data(ttl=60)
# --- FETCH SPX HISTORY (With dynamic timeframe translation!) ---
@st.cache_data(ttl=60)
def get_spx_history(period="1d", interval="5m"):
    # 1. Translate our simple buttons into Schwab's strict API requirements
    if period == "1d":
        p_type, p_val, f_type, f_val = "day", 1, "minute", 5
    elif period == "3d":
        p_type, p_val, f_type, f_val = "day", 3, "minute", 5
    elif period == "5d":
        p_type, p_val, f_type, f_val = "day", 5, "minute", 5
    elif period == "1mo":
        p_type, p_val, f_type, f_val = "month", 1, "daily", 1
    elif period == "3mo":
        p_type, p_val, f_type, f_val = "month", 3, "daily", 1
    elif period == "6mo":
        p_type, p_val, f_type, f_val = "month", 6, "daily", 1
    else:
        p_type, p_val, f_type, f_val = "day", 1, "minute", 5 # Fallback

    # 2. Call the upgraded Schwab client
    history_data = schwab_client.fetch_price_history(
        symbol="$SPX", 
        period_type=p_type, 
        period=p_val, 
        freq_type=f_type, 
        freq=f_val
    )
    
    if history_data and 'candles' in history_data:
        df = pd.DataFrame(history_data['candles'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        
        # If it's a daily chart, strip the exact time off so the x-axis stays clean
        if f_type == "daily":
            df['datetime'] = df['datetime'].dt.normalize()
            
        df.set_index('datetime', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df
        
    return pd.DataFrame() # Empty fallback

# --- FETCH LIVE OPTIONS DATA ---
@st.cache_data(ttl=60)
def get_spx_puts():
    chain_data = schwab_client.fetch_options_chain("$SPX")
    
    if not chain_data or 'putExpDateMap' not in chain_data:
        return pd.DataFrame()
        
    put_map = chain_data['putExpDateMap']
    if not put_map:
        return pd.DataFrame()
        
    # Grab the closest expiration date key (e.g. "2026-03-09:0")
    closest_exp_date = sorted(put_map.keys())[0]
    closest_puts = put_map[closest_exp_date]
    
    # Flatten Schwab's nested dictionary into a list of options
    put_list = []
    for strike, strike_data in closest_puts.items():
        option = strike_data[0] # The first item in the list is the option data
        put_list.append({
            'strike': float(strike),
            'lastPrice': option['last'] if option['last'] > 0 else option['mark'], # Fallback to mark if no last trade
            'bid': option['bid'],
            'ask': option['ask']
        })
        
    # Create the DataFrame and sort closest to the money first
    df = pd.DataFrame(put_list)
    df = df.sort_values(by='strike', ascending=False).reset_index(drop=True)
    return df

# Actually call the function to get the data using our global spx_last variable
live_puts_df = get_spx_puts()

# 4. Main Layout Columns
col_left, col_right = st.columns([1.3, 2.7], gap="medium")

with col_left:
    st.markdown('<div class="section-label">Metrics</div>', unsafe_allow_html=True)
    with st.container(border=True):
        m1, m2, m3 = st.columns(3)
        # These are live variables, so we use the f-string formatting
        m1.metric(label="SPX Open", value=f"{spx_open:,.0f}")
        m2.metric(label="SPX Last", value=f"{spx_last:,.0f}")
        
        # --- CUSTOM PILL: Change from open ---
        with m3:
            st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Change from open</p>', unsafe_allow_html=True)
            pts_change = spx_last - spx_open
            pct_change = (pts_change / spx_open) * 100
            bg = "#6DF08C" if pts_change >= 0 else "#FF4646"
            text = "#000000" if pts_change >= 0 else "#FFFFFF"
            arr = "↑" if pts_change >= 0 else "↓"
            st.markdown(f'<div style="background-color: {bg}; color: {text}; padding: 4px 8px; border-radius: 8px; display: inline-block; font-weight: 400; font-size: 12px; margin-top: 10px;">{arr} {abs(pts_change):.2f} pts ({abs(pct_change):.2f}%)</div>', unsafe_allow_html=True)
        
        st.write("")
        
        m4, m5, m6 = st.columns(3)
        # Now these are using live data!
        m4.metric(label="SPX Prior Close", value=f"{spx_prior_close:,.0f}") 
        v1, v2 = m5.columns(2)
        v1.metric(label="VIX9D", value=f"{vix9d_last:,.0f}") 
        v2.metric(label="VIX", value=f"{vix_last:,.0f}") 
        
        # --- CUSTOM PILL: Change from prior close ---
        with m6:
            st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Change from prior close</p>', unsafe_allow_html=True)
            prior_pts = spx_last - spx_prior_close
            prior_pct = (prior_pts / spx_prior_close) * 100
            bg2 = "#6DF08C" if prior_pts >= 0 else "#FF4646"
            text2 = "#000000" if prior_pts >= 0 else "#FFFFFF"
            arr2 = "↑" if prior_pts >= 0 else "↓"
            st.markdown(f'<div style="background-color: {bg2}; color: {text2}; padding: 4px 8px; border-radius: 8px; display: inline-block; font-weight: 400; font-size: 12px; margin-top: 10px;">{arr2} {abs(prior_pts):.2f} pts ({abs(prior_pct):.2f}%)</div>', unsafe_allow_html=True)

        st.write("")
        
        # --- THE CALLBACK: Forces the math to run only when inputs are changed ---
        def update_contracts():
            bp = st.session_state.saved_bp
            sw = st.session_state.saved_spread
            st.session_state.saved_contracts = int(bp / (sw * 100))

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
        
        # Dynamic Highlight Logic
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
        
        # --- THE SCROLLABLE FIX ---
        # Added height=350 to lock the container size and allow internal scrolling
        selection_event = st.dataframe(
            styled_df, 
            hide_index=True, 
            use_container_width=True,
            height=350, 
            on_select="rerun",
            selection_mode="single-row"
        )

        # --- NEW: Save the selection to permanent memory! ---
        if len(selection_event.selection.rows) > 0:
            selected_idx = selection_event.selection.rows[0]
            current_short = df_spreads.iloc[selected_idx]['Strike']
            
            # THE FIX: Only overwrite the input boxes if you clicked a DIFFERENT spread!
            if current_short != st.session_state.last_selected_short:
                new_px = float(df_spreads.iloc[selected_idx]['Spread'])
                
                st.session_state.selected_spread_px = new_px
                st.session_state.selected_short = current_short
                st.session_state.selected_long = df_spreads.iloc[selected_idx]['Leg']
                
                # Overwrite the inputs with the new defaults
                st.session_state.saved_entry = new_px
                st.session_state.saved_close = float(math.ceil(new_px * 20) / 20.0) if new_px > 0 else 0.00
                
                # Update the tracker so it doesn't loop next time!
                st.session_state.last_selected_short = current_short
                
        # Pull the values BACK OUT of memory to use in the rest of the app
        selected_short = st.session_state.selected_short
        selected_long = st.session_state.selected_long
        selected_spread_px = st.session_state.selected_spread_px

    # --- CURRENT TRADE SECTION ---
    st.write("") 
    st.markdown('<div class="section-label">Current trade</div>', unsafe_allow_html=True)
    
    with st.container(border=True):
        col1, col2, col3, col4 = st.columns(4)
        
        # THE FIX: We removed 'value=...' so Streamlit strictly relies on the 'key' memory!
        entry_px = col1.number_input("Entry PX", step=0.05, key="saved_entry")
        
        # Current PX is disabled and not tied to memory, so it keeps its value parameter
        col2.number_input("Current PX", value=float(selected_spread_px), disabled=True)
        
        # THE FIX: Removed 'value=...' here too!
        realistic_close = col3.number_input("Realistic Close", step=0.05, key="saved_close")
        
        realistic_pl = (entry_px - realistic_close) * contracts * 100
        
        pl_string = f"+${realistic_pl:,.0f}" if realistic_pl >= 0 else f"-${abs(realistic_pl):,.0f}"
        
        col4.text_input("Realistic P/L", value=pl_string, disabled=True)

with col_right:
    st.markdown('<div class="section-label">Charts</div>', unsafe_allow_html=True)

    # 2. THE CHART DRAWING ENGINE
    def create_spx_chart(title, prices, dates, line_color, halo_color):
        fig = go.Figure() # This creates the canvas!
        
        # Main line trace
        fig.add_trace(go.Scatter(
            x=dates, y=prices, mode='lines', 
            line=dict(color=line_color, width=2),
            showlegend=False
        ))
        
        # Add the "Current Position" glowing dot
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
            
        # Calculate 5% breathing room
        if len(dates) > 0:
            min_date = dates.min()
            max_date = dates.max()
            date_range = max_date - min_date
            padded_max_date = max_date + (date_range * 0.05)
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
            
        # Layout rules
        fig.update_layout(
            dragmode="zoom",
            uirevision="constant", 
            height=350, 
            margin=dict(l=60, r=20, t=10, b=30), 
            plot_bgcolor="white", paper_bgcolor="white",
            hovermode="x unified",
            hoverdistance=-1,
            spikedistance=-1,
            hoverlabel=dict(
                bgcolor="rgba(255, 255, 255, 0.85)", 
                bordercolor="rgba(0, 0, 0, 0)",
                font=dict(color="#1E1E1E")
            ),
            xaxis=dict(
                showgrid=True, gridcolor="#F0F0F0", range=[min_date, padded_max_date] if min_date else None,
                showspikes=True, spikemode="across", spikesnap="cursor", spikedash="1, 3",     
                spikecolor="#B2B2B2", spikethickness=1
            ),
            yaxis=dict(
                automargin=False, 
                showgrid=True, gridcolor="#F0F0F0", side="left",
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
            
            df_day = get_spx_history(period=day_params[selected_option], interval="5m")
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
    @st.fragment(run_every=60)
    def render_month_chart():
        # We define the variables safely inside the soundproof room!
        month_params = {"6 Months": "6mo","3 Months": "3mo","1 Month": "1mo"}
        
        with st.container(border=True):
            # This puts the button inside the box and collapses the title text!
            selected_option = st.radio(
                "Historical Timeframe", 
                list(month_params.keys()), 
                horizontal=True, 
                key="month_radio",
                label_visibility="collapsed"
            )
            
            df_month = get_spx_history(period=month_params[selected_option], interval="1d") 
            f_last, f_open, f_prior, f_delta = get_spx_metrics()
            
            is_spx_down = (f_last - f_open) < 0
            spx_theme_color = "#FF3D54" if is_spx_down else "#11F185" 
            spx_halo_color = 'rgba(255, 61, 84, 0.3)' if is_spx_down else 'rgba(17, 241, 133, 0.3)'
            
            st.plotly_chart(
                create_spx_chart(selected_option, df_month['Close'], df_month.index, spx_theme_color, spx_halo_color), 
                use_container_width=True,
                key="month_spx_chart",
                config={'displayModeBar': False} 
            )

    # DRAW MONTH CHART
    render_month_chart()