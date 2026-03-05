import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import datetime
import pytz
import yfinance as yf
import math
from streamlit_autorefresh import st_autorefresh

# --- AUTO-REFRESH TIMER ---
# Refreshes the page every 60 seconds (60000 milliseconds)
st_autorefresh(interval=60000, limit=None, key="dashboard_refresh")

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

# 3. YFinance Data Fetching Logic
@st.cache_data(ttl=60) # Refreshes metrics every minute
def get_spx_metrics():
    try:
        spx = yf.Ticker("^GSPC")
        todays_data = spx.history(period="1d")
        spx_last = todays_data['Close'].iloc[-1]
        spx_open = todays_data['Open'].iloc[0]
        pts_change = spx_last - spx_open
        pct_change = (pts_change / spx_open) * 100
        
        # Format string for the green pill
        arrow = "↑" if pts_change >= 0 else "↓"
        delta_string = f"{arrow} {abs(pts_change):.2f} pts ({pct_change:.2f}%)"
        return spx_last, spx_open, delta_string
    except Exception:
        return 6850.00, 6860.00, "0 pts (0%)" # Fallback if API fails

@st.cache_data(ttl=300) # Refreshes charts every 5 minutes
def get_spx_history(period, interval):
    spx = yf.Ticker("^GSPC")
    return spx.history(period=period, interval=interval)

spx_last, spx_open, delta_string = get_spx_metrics()

# 1. Fetch the data (last 5 days to ensure we have yesterday's close)
spx_data = yf.Ticker("^GSPC").history(period="5d")
vix_data = yf.Ticker("^VIX").history(period="1d")
vix9d_data = yf.Ticker("^VIX9D").history(period="1d")

# 2. Extract the exact numbers you need
spx_prior_close = spx_data['Close'].iloc[-2]  # The second to last item is yesterday's close
vix_last = vix_data['Close'].iloc[-1]
vix9d_last = vix9d_data['Close'].iloc[-1]

# --- FETCH LIVE OPTIONS DATA ---
@st.cache_data(ttl=60) # Caches the data for 60 seconds to keep the app fast
def get_spx_puts(current_price):
    spx = yf.Ticker("^SPX")
    expirations = spx.options
    
    # Safety check in case the market is closed/data is missing
    if not expirations:
        return pd.DataFrame()
        
    # Grab the closest expiration date (0DTE or next available)
    chain = spx.option_chain(expirations[0])
    puts = chain.puts
    
    # Filter for Out-of-the-Money (OTM) puts only, and sort closest to the money first
    otm_puts = puts[puts['strike'] < current_price].sort_values(by='strike', ascending=False)
    return otm_puts

# Actually call the function to get the data
live_puts_df = get_spx_puts(spx_last)

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
        
        in1, in2 = st.columns(2)
        buying_power = in1.number_input("Buying power ($)", value=150000, step=10000, format="%d")
        spread_width = in2.selectbox("Spread width", [10, 25, 50, 100], index=2)
        
        # --- THE MATH FOR CONTRACTS ---
        # Buying Power / (Spread Width * 100)
        calc_contracts = int(buying_power / (spread_width * 100))
        
        in3, in4 = st.columns(2)
        contracts = in3.number_input("Contracts", value=calc_contracts, step=1)
        target_profit = in4.number_input("Target profit ($)", value=1500, step=100)

    st.write("")
    st.markdown('<div class="section-label">Spreads</div>', unsafe_allow_html=True)
    with st.container(border=True):
        spreads_list = []
        
        # 1. Loop through ALL real, live OTM puts to build our master list
        for index, short_put in live_puts_df.iterrows():
            short_strike = short_put['strike']
            long_strike = short_strike - spread_width
            
            # Search the chain to see if the long leg exists
            long_put_match = live_puts_df[live_puts_df['strike'] == long_strike]
            
            if not long_put_match.empty:
                long_put = long_put_match.iloc[0]
                
                # Extract the live prices
                short_px = short_put['lastPrice']
                long_px = long_put['lastPrice']
                spread_price = short_px - long_px
                
                # Free data sometimes has stale pricing resulting in negative spreads. Skip those!
                if spread_price <= 0:
                    continue
                    
                # Calculate the math
                pts_out = abs(short_strike - spx_last)
                pct_out = (pts_out / spx_last) * 100
                total_premium = spread_price * contracts * 100
                
                spreads_list.append({
                    "Pts": int(pts_out),
                    "(%)": f"{pct_out:.1f}%",
                    "Strike": int(short_strike),
                    "Leg": int(long_strike),
                    "Short PX": short_px,
                    "Long PX": long_px,
                    "Spread": spread_price,
                    "Premiums": total_premium
                })
                
        # 2. Find the "Goldilocks" index (lowest risk that still meets the target)
        optimal_index = 0
        for i, spread in enumerate(spreads_list):
            if spread['Premiums'] >= target_profit:
                optimal_index = i
            else:
                break # The moment we dip below the target profit, we stop searching!
                
        # 3. Slice the list to center the table around our target
        # Show 5 rows above (riskier) and 5 rows below (safer/fails target)
        start_idx = max(0, optimal_index - 5)
        end_idx = min(len(spreads_list), optimal_index + 6)
        
        # Create the dataframe from just that optimized slice!
        df_spreads = pd.DataFrame(spreads_list[start_idx:end_idx])
        
        # 2. Dynamic Highlight Logic: Highlight ANY row that hits the Target Profit
        def highlight_target(row):
            if row['Premiums'] >= target_profit: 
                return ['background-color: #E4FF7A; color: black; font-weight: bold'] * len(row)
            return [''] * len(row)
            
        # 3. Format the table neatly
        styled_df = df_spreads.style.format({
            "Short PX": "{:.2f}",
            "Long PX": "{:.2f}",
            "Spread": "{:.2f}",
            "Premiums": "${:,.0f}" # Formats the raw math number into a nice currency string
        }).apply(highlight_target, axis=1)
        
        selection_event = st.dataframe(
            styled_df, 
            hide_index=True, 
            use_container_width=True,
            on_select="rerun",           # This tells the app to refresh when a box is checked!
            selection_mode="single-row"
        )
        
        # --- NEW: Grab the price of the selected spread ---
        selected_spread_px = 0.00
        if len(selection_event.selection.rows) > 0:
            selected_idx = selection_event.selection.rows[0]
            selected_spread_px = df_spreads.iloc[selected_idx]['Spread']
        
        selected_short = None
        selected_long = None
        if len(selection_event.selection.rows) > 0:
            selected_idx = selection_event.selection.rows[0]
            selected_short = df_spreads.iloc[selected_idx]['Strike']
            selected_long = df_spreads.iloc[selected_idx]['Leg']

    # --- CURRENT TRADE SECTION ---
    st.write("") 
    st.markdown('<div class="section-label">Current trade</div>', unsafe_allow_html=True)
    
    # The Math Trick: Round current price UP to the nearest 0.05 increment
    default_realistic_close = math.ceil(selected_spread_px * 20) / 20.0
    
    with st.container(border=True):
        col1, col2, col3, col4 = st.columns(4)
        
        entry_px = col1.number_input("Entry PX", value=float(selected_spread_px), step=0.05)
        col2.number_input("Current PX", value=float(selected_spread_px), disabled=True)
        
        # Drop our dynamic default value right into the input!
        realistic_close = col3.number_input("Realistic Close", value=float(default_realistic_close), step=0.05)
        
        realistic_pl = (entry_px - realistic_close) * contracts * 100
        
        pl_string = f"+${realistic_pl:,.0f}" if realistic_pl >= 0 else f"-${abs(realistic_pl):,.0f}"
        
        col4.text_input("Realistic P/L", value=pl_string, disabled=True)

with col_right:
    # 1. Update function signature to accept color parameters
    def create_spx_chart(title, prices, dates, line_color, halo_color):
        fig = go.Figure()
        
        # 2. Main line trace (Now using dynamic line_color)
        fig.add_trace(go.Scatter(
            x=dates, y=prices, mode='lines', 
            line=dict(color=line_color, width=2), # <-- Updated!
            showlegend=False
        ))
        
        # 3. Add the "Current Position" glowing dot (Now using dynamic colors)
        if len(dates) > 0 and len(prices) > 0:
            last_date = dates[-1]
            last_price = prices.iloc[-1]
            fig.add_trace(go.Scatter(
                x=[last_date], y=[last_price], mode='markers',
                marker=dict(
                    color=line_color, # <-- Updated!
                    size=4, 
                    # Halo color (30% opacity)
                    line=dict(color=halo_color, width=8) # <-- Updated!
                ),
                showlegend=False,
                hoverinfo='skip'
            ))
            
        # ... Rest of chart logic (breathing room, strike lines, layout) ...
        min_date = dates.min()
        max_date = dates.max()
        date_range = max_date - min_date
        padded_max_date = max_date + (date_range * 0.05)
        
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
            
        fig.update_layout(
            dragmode="pan",
            uirevision="constant", # <-- THE MAGIC ZOOM SAVER!
            height=350, 
            # --- FIXED MARGINS TO PREVENT THE SQUISH BUG ---
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
                showgrid=True, gridcolor="#F0F0F0", range=[min_date, padded_max_date],
                showspikes=True, spikemode="across", spikesnap="cursor", spikedash="1, 3",     
                spikecolor="#B2B2B2", spikethickness=1
            ),
            yaxis=dict(
                automargin=False, # <-- STOPS PLOTLY FROM OVER-CALCULATING ON SCROLL
                showgrid=True, gridcolor="#F0F0F0", side="left",
                showspikes=True, spikemode="across", spikesnap="cursor", spikedash="1, 3",     
                spikecolor="#B2B2B2", spikethickness=1
            )
        )
        return fig
    

    # 4. Day Chart Section
    day_option = st.segmented_control("Day Chart Options", ["5 Day chart", "3 Day chart", "1 Day chart"], default="5 Day chart", label_visibility="collapsed")
    
    if day_option is None:
        day_option = "5 Day chart"
    
    day_params = {"5 Day chart": "5d", "3 Day chart": "3d", "1 Day chart": "1d"}
    df_day = get_spx_history(period=day_params[day_option], interval="5m")


    # --- NEW: Chart Color Logic based on "Change from open" ---
    is_spx_down = (spx_last - spx_open) < 0
    spx_theme_color = "#FF4646" if is_spx_down else "#11F185" # <-- Updated to your specific red!
    # Specific RGBA red (255, 70, 70) at 30% opacity for the halo
    spx_halo_color = 'rgba(255, 70, 70, 0.3)' if is_spx_down else 'rgba(17, 241, 133, 0.3)'

    with st.container(border=True):
        st.plotly_chart(
            create_spx_chart(day_option, df_day['Close'], df_day.index, spx_theme_color, spx_halo_color), 
            use_container_width=True,
            # --- NEW: Added scrollZoom to the config! ---
            config={'displayModeBar': False, 'scrollZoom': True} 
        )

    # 5. Month Chart Section
    month_option = st.segmented_control("Month Chart Options", ["6 Month SPX", "3 Month SPX", "1 Month SPX"], default="6 Month SPX", label_visibility="collapsed")
    
    if month_option is None:
        month_option = "6 Month SPX"
    
    month_params = {"6 Month SPX": "6mo", "3 Month SPX": "3mo", "1 Month SPX": "1mo"}
    df_month = get_spx_history(period=month_params[month_option], interval="1d")

    with st.container(border=True):
        st.plotly_chart(
            create_spx_chart(month_option, df_month['Close'], df_month.index, spx_theme_color, spx_halo_color), 
            use_container_width=True,
            # --- NEW: Added scrollZoom to the config! ---
            config={'displayModeBar': False, 'scrollZoom': True} 
        )