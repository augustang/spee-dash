"""Active-trading page: live quotes, spreads, current trade, intraday + 12mo charts."""
import time

import pandas as pd
import streamlit as st

import schwab_client
from shared.chart import create_spx_chart
from shared.events import get_financial_events
from shared.header import render_header

render_header(current_page="trading")

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


# --- LIVE SCHWAB DATA FETCHING ---
@st.cache_data(ttl=60, show_spinner=False)
def get_spx_metrics():
    try:
        quote_data = schwab_client.fetch_live_quote("$SPX")
        if quote_data:
            spx_last = quote_data['lastPrice']
            spx_open = quote_data['openPrice']
            spx_prior = quote_data['closePrice']
            pts_change = quote_data['netChange']
            pct_change = (pts_change / spx_open) * 100

            arrow = "↑" if pts_change >= 0 else "↓"
            delta_string = f"{arrow} {abs(pts_change):.2f} pts ({abs(pct_change):.2f}%)"
            return spx_last, spx_open, spx_prior, delta_string
    except Exception:
        pass

    return 6850.00, 6860.00, 6800.00, "0 pts (0%)"


spx_last, spx_open, spx_prior_close, delta_string = get_spx_metrics()


@st.fragment(run_every=10)
def render_top_metrics():
    f_last, f_open, f_prior, f_delta = get_spx_metrics()


vix_quote = schwab_client.fetch_live_quote("$VIX")
vix9d_quote = schwab_client.fetch_live_quote("$VIX9D")

vix_last = vix_quote['lastPrice'] if vix_quote else 0.00
vix9d_last = vix9d_quote['lastPrice'] if vix9d_quote else 0.00


# --- FETCH SPX HISTORY: INTRADAY (1d/3d/5d) ---
@st.cache_data(ttl=60, show_spinner=False)
def get_spx_history_intraday(period="1d"):
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

# --- MAIN LAYOUT ---
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

        def update_contracts():
            bp = st.session_state.saved_bp
            sw = st.session_state.saved_spread
            st.session_state.saved_contracts = int(bp / (sw * 100))
            st.session_state.saved_target = int(st.session_state.saved_contracts * 0.10 * 100)

        in1, in2 = st.columns(2)
        buying_power = in1.number_input("Buying power ($)", step=10000, format="%d", key="saved_bp", on_change=update_contracts)
        spread_width = in2.selectbox("Spread width", [10, 25, 50, 100], key="saved_spread", on_change=update_contracts)

        in3, in4 = st.columns(2)
        contracts = in3.number_input("Contracts", step=1, key="saved_contracts")
        target_profit = in4.number_input("Target profit ($)", step=100, key="saved_target")

    st.write("")
    st.markdown('<div class="section-label">Spreads</div>', unsafe_allow_html=True)
    with st.container(border=True):
        spreads_list = []
        seen_strikes = set()

        target_pcts = [x / 10.0 for x in range(5, 81)]

        if not live_puts_df.empty:
            for pct in target_pcts:
                target_price = spx_last * (1 - (pct / 100))

                closest_idx = (live_puts_df['strike'] - target_price).abs().idxmin()
                short_put = live_puts_df.loc[closest_idx]
                short_strike = short_put['strike']

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

    @st.fragment(run_every=60)
    def render_day_chart():
        day_params = {"1 Day": "1d", "3 Days": "3d", "5 Days": "5d"}

        with st.container(border=True):
            selected_option = st.pills(
                "Intraday Timeframe",
                list(day_params.keys()),
                default="1 Day",
                key="day_radio",
                label_visibility="collapsed",
            )

            df_day = get_spx_history_intraday(period=day_params[selected_option])
            f_last, f_open, f_prior, f_delta = get_spx_metrics()

            is_spx_down = (f_last - f_open) < 0
            spx_theme_color = "#FF3D54" if is_spx_down else "#11F185"
            spx_halo_color = 'rgba(255, 61, 84, 0.3)' if is_spx_down else 'rgba(17, 241, 133, 0.3)'

            st.plotly_chart(
                create_spx_chart(
                    selected_option, df_day['Close'], df_day.index,
                    spx_theme_color, spx_halo_color,
                    selected_short=selected_short, selected_long=selected_long,
                ),
                use_container_width=True,
                key="day_spx_chart",
                config={'displayModeBar': False}
            )

    render_day_chart()

    @st.fragment(run_every=120)
    def render_month_chart():
        month_params = {"12 Months": "12mo", "8 Months": "8mo", "6 Months": "6mo", "3 Months": "3mo", "1 Month": "1mo"}

        with st.container(border=True):
            radio_col, ev_col, line_col = st.columns([3, 0.5, 0.5])
            with radio_col:
                selected_option = st.pills(
                    "Historical Timeframe",
                    list(month_params.keys()),
                    default="6 Months",
                    key="month_radio",
                    label_visibility="collapsed",
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
                create_spx_chart(
                    selected_option, df_month['Close'], df_month.index,
                    spx_theme_color, spx_halo_color,
                    events=events, chart_height=500, view_range=view_start,
                    ohlc_df=candle_data,
                    selected_short=selected_short, selected_long=selected_long,
                ),
                use_container_width=True,
                key="month_spx_chart",
                config={'displayModeBar': False}
            )

    render_month_chart()
