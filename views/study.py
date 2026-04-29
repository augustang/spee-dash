"""Study page: long-term historical chart, lazy intraday explorer, day stats,
event-impact analysis, and two-date comparison overlay."""
import datetime
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import os

import schwab_client
from shared.chart import create_spx_chart
from shared.events import get_financial_events
from shared.header import render_header

render_header(current_page="study")

# Schwab's minute-history is a rolling ~270-day window. We pad ~30 days for safety.
MINUTE_HISTORY_DAYS = 240

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# FirstRateData (SPX index, full exchange data — purchase at firstratedata.com).
# Earliest date once the full file is present: 2008-01-02.
_FRD_MIN_DATE = datetime.date(2008, 1, 2)
_FRD_5MIN_PATHS = [
    os.path.join(_DATA_DIR, "SPX_5min.csv"),         # full purchase file
    os.path.join(_DATA_DIR, "SPX_5min_sample.csv"),  # free 2-week sample
]

# ES futures CSV from Kaggle (ES E-mini, tracks SPX closely, Aug 2019 – Aug 2024).
_ES_MIN_DATE = datetime.date(2019, 8, 11)
_ES_5MIN_PATH = os.path.join(_DATA_DIR, "ES_5Years_8_11_2024.csv")


@st.cache_data(ttl=None, show_spinner=False)
def _load_frd_5min() -> pd.DataFrame:
    """Load the FirstRateData 5-min CSV (whichever file exists). Cached for the session."""
    for path in _FRD_5MIN_PATHS:
        if os.path.exists(path):
            df = pd.read_csv(path, parse_dates=["timestamp"])
            df.set_index("timestamp", inplace=True)
            df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
            df.index = df.index.tz_localize(None)
            return df
    return pd.DataFrame()


@st.cache_data(ttl=None, show_spinner=False)
def _load_es_5min() -> pd.DataFrame:
    """Load the ES futures 5-min CSV filtered to regular trading hours (09:30–16:00 ET)."""
    if not os.path.exists(_ES_5MIN_PATH):
        return pd.DataFrame()
    df = pd.read_csv(_ES_5MIN_PATH, parse_dates=["Time"])
    df.set_index("Time", inplace=True)
    df.index = df.index.tz_localize(None)
    df.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close"}, inplace=True)
    df = df.between_time("09:30", "16:00")[["Open", "High", "Low", "Close"]]
    return df


# --- LONG-TERM DAILY FETCHER ---
@st.cache_data(ttl=86400, show_spinner=False)
def get_spx_daily(years: int) -> pd.DataFrame:
    """Fetch daily SPX candles for the last `years` years (e.g. 1, 2, 5, 10, 20)."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 86400 * 1000 * 365 * years

    raw = schwab_client.fetch_price_history(
        symbol="$SPX", period_type="year", freq_type="daily", freq=1,
        start_date=start_ms, end_date=now_ms,
    )
    if raw and 'candles' in raw:
        df = pd.DataFrame(raw['candles'])
        if df.empty:
            return df
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df['datetime'] = (
            df['datetime']
            .dt.tz_localize('UTC')
            .dt.tz_convert('America/New_York')
            .dt.tz_localize(None)
            .dt.normalize()
        )
        df.set_index('datetime', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df
    return pd.DataFrame()


# --- LAZY 5-MIN INTRADAY FETCHER (one call per date, cached forever) ---
@st.cache_data(ttl=None, show_spinner="Loading 5-minute candles…")
def get_spx_5min_for_date(d: datetime.date) -> pd.DataFrame:
    # 1. Try the FirstRateData CSV first (SPX index, most accurate).
    frd = _load_frd_5min()
    if not frd.empty:
        day_df = frd[frd.index.date == d]
        if not day_df.empty:
            return day_df[["Open", "High", "Low", "Close"]]

    # 2. Try the ES futures CSV (tracks SPX closely; covers Aug 2019 – Aug 2024).
    es = _load_es_5min()
    if not es.empty:
        day_df = es[es.index.date == d]
        if not day_df.empty:
            return day_df

    # 3. Fall back to the Schwab API (rolling ~9-month window for recent dates).
    start_dt = datetime.datetime.combine(d, datetime.time(0, 0))
    end_dt = datetime.datetime.combine(d, datetime.time(23, 59, 59))
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    raw = schwab_client.fetch_price_history(
        symbol="$SPX", period_type="day", freq_type="minute", freq=5,
        start_date=start_ms, end_date=end_ms,
    )
    if raw and 'candles' in raw:
        df = pd.DataFrame(raw['candles'])
        if df.empty:
            return df
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df['datetime'] = (
            df['datetime']
            .dt.tz_localize('UTC')
            .dt.tz_convert('America/New_York')
            .dt.tz_localize(None)
        )
        df = df[df['datetime'].dt.date == d]
        if df.empty:
            return df
        df.set_index('datetime', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        return df
    return pd.DataFrame()


# =========================
# 1. LONG-TERM CHART
# =========================
st.markdown('<div class="section-label">Long-term chart</div>', unsafe_allow_html=True)
with st.container(border=True):
    range_params = {"1Y": 1, "2Y": 2, "5Y": 5, "10Y": 10, "Max": 20}

    radio_col, ev_col, line_col = st.columns([3, 0.5, 0.5])
    with radio_col:
        selected_range = st.radio(
            "Range",
            list(range_params.keys()),
            index=0,
            horizontal=True,
            key="study_range_radio",
            label_visibility="collapsed",
        )
    with ev_col:
        show_events = st.checkbox("Events", key="study_show_events")
    with line_col:
        show_line = st.checkbox("Line", key="study_show_line")

    years = range_params[selected_range]
    df_long = get_spx_daily(years)

    if df_long.empty:
        st.info("No long-term data available.")
    else:
        last_close = float(df_long['Close'].iloc[-1])
        first_open = float(df_long['Open'].iloc[0])
        is_down = (last_close - first_open) < 0
        line_color = "#FF3D54" if is_down else "#11F185"
        halo_color = 'rgba(255, 61, 84, 0.3)' if is_down else 'rgba(17, 241, 133, 0.3)'

        events = None
        if show_events:
            lookahead = df_long.index.max() + pd.DateOffset(months=1)
            events = get_financial_events(df_long.index.min(), lookahead)

        candle_data = None if show_line else df_long
        st.plotly_chart(
            create_spx_chart(
                selected_range,
                df_long['Close'],
                df_long.index,
                line_color,
                halo_color,
                events=events,
                chart_height=680,
                ohlc_df=candle_data,
            ),
            use_container_width=True,
            key="study_long_chart",
            config={'displayModeBar': False},
        )


# =========================
# 2. INTRADAY CARD + DAY STATS
# =========================
st.write("")
st.markdown('<div class="section-label">Intraday explorer</div>', unsafe_allow_html=True)

today = datetime.date.today()
frd_loaded = os.path.exists(os.path.join(_DATA_DIR, "SPX_5min.csv"))
es_loaded  = not _load_es_5min().empty
if frd_loaded:
    min_date = _FRD_MIN_DATE
elif es_loaded:
    min_date = _ES_MIN_DATE
else:
    min_date = today - datetime.timedelta(days=MINUTE_HISTORY_DAYS)

col_stats, col_chart = st.columns([1.3, 2.7], gap="medium")

with col_stats:
    with st.container(border=True):
        pick_col, _ = st.columns([2, 1])
        with pick_col:
            selected_date = st.date_input(
                "Day to study",
                value=today - datetime.timedelta(days=1),
                min_value=min_date,
                max_value=today,
                key="study_intraday_date",
            )

        df_day = get_spx_5min_for_date(selected_date)

        if df_day.empty:
            st.info(
                f"No data for {selected_date.strftime('%b %-d, %Y')}. "
                "Try a US trading day within the last ~9 months."
            )
        else:
            day_open  = float(df_day['Open'].iloc[0])
            day_high  = float(df_day['High'].max())
            day_low   = float(df_day['Low'].min())
            day_close = float(df_day['Close'].iloc[-1])

            # Row 1: Open, Close
            m1, m2 = st.columns(2)
            m1.metric(label="Open",  value=f"{day_open:,.2f}")
            m2.metric(label="Close", value=f"{day_close:,.2f}")

            # Row 2: High, Low
            m3, m4 = st.columns(2)
            m3.metric(label="High", value=f"{day_high:,.2f}")
            m4.metric(label="Low",  value=f"{day_low:,.2f}")

            st.markdown('<div style="margin-top: 12px;"></div>', unsafe_allow_html=True)

            # Day change pill
            day_change_pts = day_close - day_open
            day_change_pct = (day_change_pts / day_open) * 100
            ch_bg   = "#6DF08C" if day_change_pts >= 0 else "#FF4646"
            ch_text = "#000000" if day_change_pts >= 0 else "#FFFFFF"
            ch_arr  = "↑" if day_change_pts >= 0 else "↓"

            # Gap vs prior close pill
            gap_pts, gap_pct = None, None
            try:
                prior_close = None

                # 1. Try Schwab daily data (already loaded for the long-term chart).
                if not df_long.empty:
                    sel_ts = pd.Timestamp(selected_date)
                    prior_days = df_long.index[df_long.index < sel_ts]
                    if len(prior_days) > 0:
                        prior_close = float(df_long.loc[prior_days[-1], 'Close'])

                # 2. Fall back to the 5-min CSV sources (already cached in memory).
                if prior_close is None:
                    for _loader in (_load_frd_5min, _load_es_5min):
                        _src = _loader()
                        if not _src.empty:
                            _prior_bars = _src[_src.index.date < selected_date]
                            if not _prior_bars.empty:
                                prior_close = float(_prior_bars['Close'].iloc[-1])
                                break

                if prior_close is not None:
                    gap_pts = day_open - prior_close
                    gap_pct = (gap_pts / prior_close) * 100
            except Exception:
                pass

            # Intraday vol
            log_rets = np.log(df_day['Close'] / df_day['Close'].shift(1)).dropna()
            vol_str = f"{log_rets.std() * np.sqrt(78) * 100:.2f}%" if len(log_rets) > 1 else "—"

            day_range_pts = day_high - day_low

            # Row 3: Day change, Overnight gap
            p1, p2 = st.columns(2)
            with p1:
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Day change</p>', unsafe_allow_html=True)
                st.markdown(
                    f'<div style="background-color:{ch_bg};color:{ch_text};padding:4px 8px;'
                    f'border-radius:8px;display:inline-block;font-weight:400;font-size:12px;margin-top:10px;">'
                    f'{ch_arr} {abs(day_change_pts):.2f} ({abs(day_change_pct):.2f}%)</div>',
                    unsafe_allow_html=True,
                )
            with p2:
                if gap_pts is not None:
                    gp_bg   = "#6DF08C" if gap_pts >= 0 else "#FF4646"
                    gp_text = "#000000" if gap_pts >= 0 else "#FFFFFF"
                    gp_arr  = "↑" if gap_pts >= 0 else "↓"
                    st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Overnight gap</p>', unsafe_allow_html=True)
                    st.markdown(
                        f'<div style="background-color:{gp_bg};color:{gp_text};padding:4px 8px;'
                        f'border-radius:8px;display:inline-block;font-weight:400;font-size:12px;margin-top:10px;">'
                        f'{gp_arr} {abs(gap_pts):.2f} ({abs(gap_pct):.2f}%)</div>',
                        unsafe_allow_html=True,
                    )

            st.markdown('<div style="margin-top: 12px;"></div>', unsafe_allow_html=True)

            # Row 4: Range, Intraday vol
            p3, p4 = st.columns(2)
            with p3:
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Range</p>', unsafe_allow_html=True)
                st.markdown(
                    f'<div style="background:#F1F2F6;padding:4px 8px;border-radius:8px;'
                    f'display:inline-block;font-size:12px;margin-top:10px;">'
                    f'{day_range_pts:.1f} pts</div>',
                    unsafe_allow_html=True,
                )
            with p4:
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Intraday vol</p>', unsafe_allow_html=True)
                st.markdown(
                    f'<div style="background:#F1F2F6;padding:4px 8px;border-radius:8px;'
                    f'display:inline-block;font-size:12px;margin-top:10px;">'
                    f'{vol_str}</div>',
                    unsafe_allow_html=True,
                )

            st.markdown('<div style="padding-bottom: 12px;"></div>', unsafe_allow_html=True)

with col_chart:
    with st.container(border=True):
        if 'df_day' not in dir() or df_day.empty:
            st.info("Pick a trading day on the left to load the 5-min chart.")
        else:
            day_open  = float(df_day['Open'].iloc[0])
            day_close = float(df_day['Close'].iloc[-1])
            is_down    = (day_close - day_open) < 0
            line_color = "#FF3D54" if is_down else "#11F185"
            halo_color = 'rgba(255, 61, 84, 0.3)' if is_down else 'rgba(17, 241, 133, 0.3)'

            st.plotly_chart(
                create_spx_chart(
                    selected_date.strftime("%b %-d, %Y"),
                    df_day['Close'],
                    df_day.index,
                    line_color,
                    halo_color,
                    chart_height=420,
                    hover_xfmt="%H:%M",
                ),
                use_container_width=True,
                key="study_intraday_chart",
                config={'displayModeBar': False},
            )


# =========================
# 3. COMPARE TWO DATES
# =========================
st.write("")
st.markdown('<div class="section-label">Compare two dates</div>', unsafe_allow_html=True)
with st.container(border=True):
    pick1_col, pick2_col, _spacer = st.columns([1.2, 1.2, 3])
    today = datetime.date.today()
    frd_loaded = os.path.exists(os.path.join(_DATA_DIR, "SPX_5min.csv"))
    es_loaded  = not _load_es_5min().empty
    if frd_loaded:
        min_date = _FRD_MIN_DATE
    elif es_loaded:
        min_date = _ES_MIN_DATE
    else:
        min_date = today - datetime.timedelta(days=MINUTE_HISTORY_DAYS)

    with pick1_col:
        date_a = st.date_input(
            "Date A",
            value=today - datetime.timedelta(days=2),
            min_value=min_date,
            max_value=today,
            key="study_compare_date_a",
        )
    with pick2_col:
        date_b = st.date_input(
            "Date B",
            value=today - datetime.timedelta(days=1),
            min_value=min_date,
            max_value=today,
            key="study_compare_date_b",
        )

    df_a = get_spx_5min_for_date(date_a)
    df_b = get_spx_5min_for_date(date_b)

    if df_a.empty and df_b.empty:
        st.info("No 5-minute candles for the selected dates. Pick US trading days within the last ~9 months.")
    else:
        # Map each series onto a shared reference date so both lines plot on
        # the same real-time axis (09:30 → 16:00) regardless of calendar date.
        _ref = datetime.date(2000, 1, 3)

        def _to_time_axis(df: pd.DataFrame):
            if df.empty:
                return [], []
            times = [datetime.datetime.combine(_ref, ts.time()) for ts in df.index]
            open_px = float(df['Open'].iloc[0])
            pct = ((df['Close'] / open_px - 1) * 100).round(2)
            return times, pct

        x_a, y_a = _to_time_axis(df_a)
        x_b, y_b = _to_time_axis(df_b)

        fig = go.Figure()
        if len(x_a) > 0:
            fig.add_trace(go.Scatter(
                x=x_a, y=y_a, mode='lines',
                name=date_a.strftime("%b %-d, %Y"),
                line=dict(color="#4B7BFF", width=2),
                hovertemplate="%{y:+.2f}%<extra></extra>",
            ))
        if len(x_b) > 0:
            fig.add_trace(go.Scatter(
                x=x_b, y=y_b, mode='lines',
                name=date_b.strftime("%b %-d, %Y"),
                line=dict(color="#B71AFF", width=2),
                hovertemplate="%{y:+.2f}%<extra></extra>",
            ))

        fig.add_hline(y=0, line_dash="dot", line_color="#B2B2B2", line_width=1)

        fig.update_layout(
            dragmode="zoom",
            uirevision="constant",
            height=560,
            margin=dict(l=60, r=20, t=10, b=30),
            plot_bgcolor="white", paper_bgcolor="white",
            hovermode="x unified",
            hoverlabel=dict(
                bgcolor="rgba(255, 255, 255, 0.85)",
                bordercolor="rgba(0, 0, 0, 0)",
                font=dict(color="#1E1E1E"),
            ),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                bgcolor="rgba(0,0,0,0)", font=dict(size=11),
            ),
            xaxis=dict(
                showgrid=True, gridcolor="#F0F0F0",
                tickformat="%H:%M",
                hoverformat="%H:%M",
                range=[
                    datetime.datetime.combine(_ref, datetime.time(9, 30)),
                    datetime.datetime.combine(_ref, datetime.time(16, 0)),
                ],
                showspikes=True, spikemode="across", spikesnap="cursor",
                spikedash="1, 3", spikecolor="#B2B2B2", spikethickness=1,
                rangeslider=dict(visible=False),
            ),
            yaxis=dict(
                automargin=False,
                showgrid=True, gridcolor="#F0F0F0", side="left",
                title=dict(text="% from open", font=dict(size=10, color="#666")),
                ticksuffix="%",
                showspikes=True, spikemode="across", spikesnap="cursor",
                spikedash="1, 3", spikecolor="#B2B2B2", spikethickness=1,
            ),
        )
        st.plotly_chart(
            fig,
            use_container_width=True,
            key="study_compare_chart",
            config={'displayModeBar': False},
        )


# =========================
# 4. EVENT IMPACT TABLE
# =========================
def _compute_event_impact(daily_df: pd.DataFrame, events: list) -> pd.DataFrame:
    """For each event type, compute count + average/median return on event day,
    prior trading day, and next trading day."""
    if daily_df.empty or not events:
        return pd.DataFrame()

    closes = daily_df['Close']
    pct_change = closes.pct_change() * 100
    trading_days = daily_df.index

    rows = []
    for evt_date, label in events:
        evt_ts = pd.Timestamp(evt_date)
        future = trading_days[trading_days >= evt_ts]
        if len(future) == 0:
            continue
        evt_day = future[0]
        evt_loc = trading_days.get_loc(evt_day)

        prior_ret = pct_change.iloc[evt_loc - 1] if evt_loc - 1 >= 0 else np.nan
        evt_ret = pct_change.iloc[evt_loc] if evt_loc < len(pct_change) else np.nan
        next_ret = pct_change.iloc[evt_loc + 1] if evt_loc + 1 < len(pct_change) else np.nan

        rows.append({
            "type": label,
            "prior": prior_ret,
            "event": evt_ret,
            "next": next_ret,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    grouped = df.groupby("type").agg(
        Count=("event", "count"),
        Prior_Avg=("prior", "mean"),
        Event_Avg=("event", "mean"),
        Event_Med=("event", "median"),
        Next_Avg=("next", "mean"),
    ).reset_index().rename(columns={
        "type": "Event",
        "Prior_Avg": "Prior day avg %",
        "Event_Avg": "Event day avg %",
        "Event_Med": "Event day median %",
        "Next_Avg": "Next day avg %",
    })
    return grouped.sort_values("Count", ascending=False).reset_index(drop=True)


st.write("")
st.markdown('<div class="section-label">Event impact</div>', unsafe_allow_html=True)
with st.container(border=True):
    if df_long.empty:
        st.info("Long-term data unavailable, can't compute event impact.")
    else:
        all_events = get_financial_events(df_long.index.min(), df_long.index.max())
        impact_df = _compute_event_impact(df_long, all_events)
        if impact_df.empty:
            st.info("No events fall in the current range.")
        else:
            def _color_returns(val):
                if pd.isna(val):
                    return ''
                color = "#11F185" if val >= 0 else "#FF3D54"
                weight = "600"
                return f'color: {color}; font-weight: {weight};'

            ret_cols = ["Prior day avg %", "Event day avg %", "Event day median %", "Next day avg %"]
            styled = impact_df.style.format({c: "{:+.2f}%" for c in ret_cols}).applymap(
                _color_returns, subset=ret_cols
            )
            st.dataframe(styled, hide_index=True, use_container_width=True)
            st.caption(f"Computed across the last {years}Y of daily closes ({len(df_long):,} trading days).")
