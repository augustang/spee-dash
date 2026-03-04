import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import datetime
import pytz
import yfinance as yf

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

# 4. Main Layout Columns
col_left, col_right = st.columns([1.3, 2.7], gap="medium")

with col_left:
    st.markdown('<div class="section-label">Metrics</div>', unsafe_allow_html=True)
    with st.container(border=True):
        m1, m2, m3 = st.columns(3)
        m1.metric(label="SPX Open", value=f"{spx_open:,.2f}")
        m2.metric(label="SPX Last", value=f"{spx_last:,.2f}")
        m3.metric(label="Change from open", value="", delta=delta_string)
        
        st.write("")
        
        m4, m5, m6 = st.columns(3)
        m4.metric(label="SPX Prior Close", value="6900.00") # Placeholder
        v1, v2 = m5.columns(2)
        v1.metric(label="VIX9D", value="19.00") # Placeholder
        v2.metric(label="VIX", value="21.00") # Placeholder
        m6.metric(label="Change from prior close", value="", delta="↑ 20 pts (0.2%)") # Placeholder

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
        # 1. Generate dynamic rows starting ~40 points out of the money
        base_strike = int(spx_last / 10) * 10 - 40 
        
        spreads_list = []
        for i in range(8): # Create spreads rows
            strike = base_strike - (i * 10)
            leg = strike - spread_width
            pts_out = strike - spx_last
            pct_out = (pts_out / spx_last) * 100
            
            # Simulating realistic option prices dropping as they get further out of the money
            short_px = max(0.50, 4.00 - (i * 0.60)) 
            long_px = max(0.10, 1.50 - (i * 0.20))
            spread_price = short_px - long_px
            
            # --- THE MATH FOR PREMIUMS ---
            # Spread price * Contracts * 100
            total_premium = spread_price * contracts * 100
            
            spreads_list.append({
                "Pts": int(pts_out),
                "(%)": f"{pct_out:.1f}%",
                "Strike": int(strike),
                "Leg": int(leg),
                "Short PX": short_px,
                "Long PX": long_px,
                "Spread": spread_price,
                "Premiums": total_premium # Store as raw number for math
            })
            
        df_spreads = pd.DataFrame(spreads_list)
        
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
            on_select="rerun",
            selection_mode="single-row"
        )
        
        selected_short = None
        selected_long = None
        if len(selection_event.selection.rows) > 0:
            selected_idx = selection_event.selection.rows[0]
            selected_short = df_spreads.iloc[selected_idx]['Strike']
            selected_long = df_spreads.iloc[selected_idx]['Leg']

    st.write("")
    st.markdown('<div class="section-label">Current trade</div>', unsafe_allow_html=True)
    with st.container(border=True):
        trade_data = {"Entry PX": [2.50], "Current PX": [2.10], "Realistic Close": [0.50], "Realistic P/L": ["+$6,000"]}
        st.dataframe(pd.DataFrame(trade_data), hide_index=True, use_container_width=True)

with col_right:
    # Chart generation function
    def create_spx_chart(title, prices, dates):
        fig = go.Figure()
        
        # 1. Main line trace
        fig.add_trace(go.Scatter(
            x=dates, y=prices, mode='lines', 
            line=dict(color='#11F185', width=2),
            showlegend=False
        ))
        
        # 2. Add the "Current Position" glowing dot
        if len(dates) > 0 and len(prices) > 0:
            last_date = dates[-1]
            last_price = prices.iloc[-1]
            fig.add_trace(go.Scatter(
                x=[last_date], y=[last_price], mode='markers',
                marker=dict(
                    color='#11F185', 
                    size=4, 
                    line=dict(color='rgba(17, 241, 133, 0.3)', width=8) # The soft halo
                ),
                showlegend=False,
                hoverinfo='skip'
            ))
            
        # 3. Calculate 5% breathing room
        min_date = dates.min()
        max_date = dates.max()
        date_range = max_date - min_date
        padded_max_date = max_date + (date_range * 0.05)
        
        # 4. Interactive strike lines
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
            
        # 5. Apply layout using the padded_max_date
        fig.update_layout(
            height=350, margin=dict(l=0, r=0, t=10, b=0), 
            plot_bgcolor="white", paper_bgcolor="white",
            xaxis=dict(showgrid=True, gridcolor="#F0F0F0", range=[min_date, padded_max_date]),
            yaxis=dict(showgrid=True, gridcolor="#F0F0F0", side="left")
        )
        return fig

    # 5 Day Segmented Control & Chart
    day_option = st.segmented_control("Day Chart Options", ["5 Day chart", "3 Day chart", "1 Day chart"], default="5 Day chart", label_visibility="collapsed")
    day_params = {"5 Day chart": "5d", "3 Day chart": "3d", "1 Day chart": "1d"}
    df_day = get_spx_history(period=day_params[day_option], interval="5m")

    with st.container(border=True):
        st.plotly_chart(
            create_spx_chart(day_option, df_day['Close'], df_day.index), 
            use_container_width=True,
            config={'displayModeBar': False} # <-- Hides the menu
        )

    # 6 Month Segmented Control & Chart
    month_option = st.segmented_control("Month Chart Options", ["6 Month SPX", "3 Month SPX", "1 Month SPX"], default="6 Month SPX", label_visibility="collapsed")
    month_params = {"6 Month SPX": "6mo", "3 Month SPX": "3mo", "1 Month SPX": "1mo"}
    df_month = get_spx_history(period=month_params[month_option], interval="1d")

    with st.container(border=True):
        st.plotly_chart(
            create_spx_chart(month_option, df_month['Close'], df_month.index), 
            use_container_width=True,
            config={'displayModeBar': False} # <-- Hides the menu
        )