"""Study page: long-term historical chart, lazy intraday explorer, day stats,
event-impact analysis, and two-date comparison overlay."""
import datetime
import json
import os
import re
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import schwab_client
from shared.chart import create_spx_chart
from shared.events import FOMC_DATES, get_financial_events
from shared.header import render_header

render_header(current_page="study")

# Load per-date notes for the Intraday Moves section.
# Edit data/intraday_notes.json to add/update notes; keys are "YYYY-MM-DD".
_NOTES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "intraday_notes.json")
try:
    with open(_NOTES_PATH) as _f:
        _INTRADAY_NOTES: dict[str, str] = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    _INTRADAY_NOTES = {}

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


@st.cache_resource
def _load_es_5min() -> pd.DataFrame:
    """Load the ES futures 5-min CSV filtered to regular trading hours (09:30–16:00 ET).
    Uses cache_resource (not cache_data) to avoid pickle overhead on the ~17MB CSV."""
    if not os.path.exists(_ES_5MIN_PATH):
        return pd.DataFrame()
    df = pd.read_csv(_ES_5MIN_PATH, parse_dates=["Time"])
    df.set_index("Time", inplace=True)
    df.index = df.index.tz_localize(None)
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


# Fixed Jan-2019-to-today daily dataset used exclusively for event impact stats.
_EVENT_IMPACT_START = datetime.date(2019, 1, 1)

@st.cache_data(ttl=86400, show_spinner=False)
def _get_event_daily_df() -> pd.DataFrame:
    """Daily SPX closes from Jan 2019 to today — fixed window for event impact stats."""
    now_ms   = int(time.time() * 1000)
    start_ms = int(datetime.datetime(_EVENT_IMPACT_START.year, 1, 1).timestamp() * 1000)
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
        selected_range = st.pills(
            "Range",
            list(range_params.keys()),
            default="1Y",
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
        selected_date = st.date_input(
            "Day to study",
            value=today - datetime.timedelta(days=1),
            min_value=min_date,
            max_value=today,
            format="MM/DD/YYYY",
            key="study_intraday_date",
        )

        st.markdown('<div style="margin-top:16px;"></div>', unsafe_allow_html=True)

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

            st.markdown('<div style="margin-top:16px;"></div>', unsafe_allow_html=True)

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

            # Realized vol: std of 5-min log returns scaled to a full session (√78 bars).
            log_rets = np.log(df_day['Close'] / df_day['Close'].shift(1)).dropna()
            if len(log_rets) > 1:
                vol_pct = log_rets.std() * np.sqrt(78) * 100
                vol_pts = vol_pct * day_open / 100
                vol_str = f"{vol_pts:.1f} pts ({vol_pct:.2f}%)"
            else:
                vol_str = "—"

            day_range_pts = day_high - day_low

            # Row 3: Day change, Overnight gap
            p1, p2 = st.columns(2)
            with p1:
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Open to close</p>', unsafe_allow_html=True)
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

            st.markdown('<div style="margin-top:16px;"></div>', unsafe_allow_html=True)

            # Row 4: Open to low, High to low
            p3, p4 = st.columns(2)
            with p3:
                ol_pts = day_low - day_open
                ol_pct = (ol_pts / day_open) * 100
                ol_sign = "+" if ol_pts >= 0 else ""
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Open to low</p>', unsafe_allow_html=True)
                st.markdown(
                    f'<div style="background:#F1F2F6;padding:4px 8px;border-radius:8px;'
                    f'display:inline-block;font-size:12px;margin-top:10px;">'
                    f'{ol_sign}{ol_pts:.1f} pts ({ol_sign}{ol_pct:.2f}%)</div>',
                    unsafe_allow_html=True,
                )
            with p4:
                hl_pts = day_low - day_high  # always negative: high → low is a downward move
                hl_pct = (hl_pts / day_high) * 100
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">High to low</p>', unsafe_allow_html=True)
                st.markdown(
                    f'<div style="background:#F1F2F6;padding:4px 8px;border-radius:8px;'
                    f'display:inline-block;font-size:12px;margin-top:10px;">'
                    f'{hl_pts:.1f} pts ({hl_pct:.2f}%)</div>',
                    unsafe_allow_html=True,
                )

            st.markdown('<div style="margin-top:16px;"></div>', unsafe_allow_html=True)

            # Row 5: Realized vol
            p5, _ = st.columns(2)
            with p5:
                st.markdown('<p style="font-size: 12px; color: #000000; margin-bottom: -10px;">Realized vol (1σ)</p>', unsafe_allow_html=True)
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
# 3. COMPARE DATES
# =========================
st.write("")
st.markdown('<div class="section-label">Compare dates</div>', unsafe_allow_html=True)

_COMPARE_COLORS = ["#B71AFF", "#4B7BFF", "#1A1A1A", "#888888", "#C8C8C8"]
_COMPARE_LABELS = ["Date A", "Date B", "Date C", "Date D", "Date E"]

with st.container(border=True):
    today = datetime.date.today()
    frd_loaded = os.path.exists(os.path.join(_DATA_DIR, "SPX_5min.csv"))
    es_loaded  = not _load_es_5min().empty
    if frd_loaded:
        min_date = _FRD_MIN_DATE
    elif es_loaded:
        min_date = _ES_MIN_DATE
    else:
        min_date = today - datetime.timedelta(days=MINUTE_HISTORY_DAYS)

    _ES_END_DATE   = datetime.date(2024, 8, 9)
    _SCHWAB_START  = today - datetime.timedelta(days=MINUTE_HISTORY_DAYS)

    def _source_label(d: datetime.date) -> tuple[str, str]:
        """Return (label, color) for the data source expected for a given date."""
        if not isinstance(d, datetime.date):
            return "", "#888"
        if frd_loaded and d >= _FRD_MIN_DATE:
            return "SPX · FirstRateData", "#444"
        if d >= _SCHWAB_START:
            return "SPX · Schwab API", "#444"
        if _ES_MIN_DATE <= d <= _ES_END_DATE:
            return "ES Futures · CSV", "#444"
        return "No data available", "#FF3D54"

    def _recent_dates(event_type: str, n: int = 5) -> list[datetime.date]:
        """Return the n most recent past dates of a given event type, newest→oldest."""
        lookback = today - datetime.timedelta(days=365 * 2)
        evts = get_financial_events(lookback, today)
        return sorted(
            [d for d, lbl in evts if event_type in lbl and d <= today],
            reverse=True,
        )[:n]

    def _last_n_weekdays(n: int = 5) -> list[datetime.date]:
        """Return the n most recent Mon–Fri days before today, newest→oldest."""
        days, d = [], today - datetime.timedelta(days=1)
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d)
            d -= datetime.timedelta(days=1)
        return days

    _PRESET_OPTIONS = ["FOMC", "OPEX", "VIX Exp", "Last 5 days"]

    _preset = st.pills(
        "Quick load",
        options=_PRESET_OPTIONS,
        default="FOMC",
        key="study_compare_preset",
        label_visibility="collapsed",
    )

    st.write("")

    # If the user deselects all pills, snap back to the last known preset.
    _effective_preset = _preset or st.session_state.get("study_compare_preset_last", "FOMC")

    # When effective preset changes, overwrite the date picker session state keys.
    if st.session_state.get("study_compare_preset_last") != _effective_preset:
        st.session_state["study_compare_preset_last"] = _effective_preset
        if _effective_preset == "FOMC":
            _preset_dates = sorted([d for d in FOMC_DATES if d <= today], reverse=True)[:5]
        elif _effective_preset == "Last 5 days":
            _preset_dates = _last_n_weekdays()
        else:
            _preset_dates = _recent_dates(_effective_preset)
        for _i in range(5):
            st.session_state[f"study_compare_date_{_i}"] = (
                _preset_dates[_i] if _i < len(_preset_dates) else None
            )

    # Seed session state on very first load (no preset change has fired yet).
    if "study_compare_preset_last" not in st.session_state:
        _seed = sorted([d for d in FOMC_DATES if d <= today], reverse=True)[:5]
        for _i in range(5):
            st.session_state[f"study_compare_date_{_i}"] = (
                _seed[_i] if _i < len(_seed) else None
            )

    _pick_cols = st.columns(5)
    _compare_dates = []
    for _i, (_col, _label) in enumerate(zip(_pick_cols, _COMPARE_LABELS)):
        with _col:
            _dot_color = _COMPARE_COLORS[_i]
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">'
                f'<span style="width:6px;height:6px;border-radius:50%;background:{_dot_color};display:inline-block;flex-shrink:0;"></span>'
                f'<span style="font-size:12px;color:#444;">{_label}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            _d = st.date_input(
                _label,
                value=None,
                min_value=min_date,
                max_value=today,
                format="MM/DD/YYYY",
                key=f"study_compare_date_{_i}",
                label_visibility="collapsed",
            )
            _compare_dates.append(_d)
            _src, _src_color = _source_label(_d if isinstance(_d, datetime.date) else None)
            if _src:
                st.markdown(
                    f'<div style="margin-top:-12px;">'
                    f'<span style="font-size:11px;color:{"#FF3D54" if _src_color == "#FF3D54" else "#888"};">{_src}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    _compare_dfs = [
        get_spx_5min_for_date(d) if isinstance(d, datetime.date) else pd.DataFrame()
        for d in _compare_dates
    ]

    _ref = datetime.date(2000, 1, 3)

    def _to_time_axis(df: pd.DataFrame):
        if df.empty:
            return [], []
        times = [datetime.datetime.combine(_ref, ts.time()) for ts in df.index]
        open_px = float(df['Open'].iloc[0])
        pct = ((df['Close'] / open_px - 1) * 100).round(2)
        return times, pct

    _empty_layout = dict(
        height=560,
        margin=dict(l=60, r=20, t=10, b=30),
        plot_bgcolor="white", paper_bgcolor="white",
        xaxis=dict(
            showgrid=True, gridcolor="#F0F0F0",
            tickformat="%H:%M",
            range=[
                datetime.datetime.combine(_ref, datetime.time(9, 30)),
                datetime.datetime.combine(_ref, datetime.time(16, 0)),
            ],
            rangeslider=dict(visible=False),
        ),
        yaxis=dict(
            showgrid=True, gridcolor="#F0F0F0", side="left",
            title=dict(text="% from open", font=dict(size=10, color="#666")),
            ticksuffix="%",
        ),
    )

    if all(df.empty for df in _compare_dfs):
        _empty_fig = go.Figure()
        _empty_fig.add_hline(y=0, line_dash="dot", line_color="#B2B2B2", line_width=1)
        _empty_fig.update_layout(**_empty_layout)
        st.plotly_chart(_empty_fig, use_container_width=True, config={'displayModeBar': False})
    else:
        fig = go.Figure()
        for _d, _df, _color, _label in zip(_compare_dates, _compare_dfs, _COMPARE_COLORS, _COMPARE_LABELS):
            if not isinstance(_d, datetime.date):
                continue
            _x, _y = _to_time_axis(_df)
            if len(_x) > 0:
                fig.add_trace(go.Scatter(
                    x=_x, y=_y, mode='lines',
                    name=_d.strftime("%b %-d, %Y"),
                    line=dict(color=_color, width=1),
                    showlegend=False,
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

    # Normalise labels: collapse "Jan OPEX", "Feb OPEX", etc. → "OPEX"
    def _norm(label):
        if "OPEX" in label:
            return "OPEX"
        return label

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
            "type": _norm(label),
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
    _event_daily = _get_event_daily_df()
    if _event_daily.empty:
        st.info("Daily data unavailable — can't compute event impact.")
    else:
        _today = datetime.date.today()
        all_events = get_financial_events(_EVENT_IMPACT_START, _today)
        impact_df = _compute_event_impact(_event_daily, all_events)
        if impact_df.empty:
            st.info("No events found in range.")
        else:
            def _color_returns(val):
                if pd.isna(val):
                    return ''
                color = "#11F185" if val >= 0 else "#FF3D54"
                return f'color: {color}; font-weight: 600;'

            ret_cols = ["Prior day avg %", "Event day avg %", "Event day median %", "Next day avg %"]
            styled = impact_df.style.format({c: "{:+.2f}%" for c in ret_cols}).applymap(
                _color_returns, subset=ret_cols
            )
            st.dataframe(styled, hide_index=True, use_container_width=True)
            st.caption(
                f"Averaged from Jan {_EVENT_IMPACT_START.year} → today · "
                f"{len(_event_daily):,} trading days · {len(all_events)} event occurrences"
            )

# =========================
# 5. KEY DATES
# =========================
st.write("")
st.markdown('<div class="section-label">Key dates</div>', unsafe_allow_html=True)

# Hand-curated notable SPX single-day moves ≥ ~3% since 2019.
# Percentages are computed live from open-to-close OHLC data.
_NOTABLE_EVENTS: list[tuple[datetime.date, str]] = [
    (datetime.date(2019, 8,  5),  "Trade war escalation"),
    (datetime.date(2020, 2, 24),  "COVID fears begin"),
    (datetime.date(2020, 2, 27),  "COVID selloff"),
    (datetime.date(2020, 3,  9),  "Black Monday II"),
    (datetime.date(2020, 3, 12),  "COVID crash"),
    (datetime.date(2020, 3, 16),  "Worst day since '87"),
    (datetime.date(2020, 3, 24),  "Biggest rally since '33"),
    (datetime.date(2020, 3, 26),  "Stimulus rally"),
    (datetime.date(2020, 4,  6),  "Stimulus rally II"),
    (datetime.date(2022, 5,  5),  "Fed hike selloff"),
    (datetime.date(2022, 6, 13),  "Bear mkt confirm"),
    (datetime.date(2022, 9, 13),  "Hot CPI shock"),
    (datetime.date(2024, 12, 18), "FOMC hawkish pivot"),
    (datetime.date(2025, 4,  3),  "Liberation Day"),
    (datetime.date(2025, 4,  4),  "Tariff panic"),
    (datetime.date(2025, 4,  9),  "Tariff pause"),
]

def _pill(d: datetime.date) -> str:
    return (
        f'<input type="text" readonly value="{d.strftime("%m/%d/%Y")}" '
        f'onclick="this.select()" '
        f'style="display:block;font-size:12px;color:#444;background:#F1F2F6;'
        f'border:none;outline:none;padding:3px 10px;border-radius:6px;'
        f'margin-bottom:4px;width:94px;cursor:default;font-family:inherit;">'
    )

def _pct_spans(oc: float | None, ol: float | None) -> str:
    """Return side-by-side open-to-close (green/red) and open-to-low (purple) spans."""
    parts = []
    if oc is not None:
        sign  = "+" if oc >= 0 else ""
        color = "#11F185" if oc >= 0 else "#FF3D54"
        parts.append(f'<span style="font-size:12px;color:{color};">{sign}{oc:.1f}%</span>')
    if ol is not None:
        parts.append(f'<span style="font-size:12px;color:#B71AFF;">{ol:.1f}%</span>')
    if not parts:
        return ""
    return (
        f'<div style="display:flex;gap:8px;margin-bottom:6px;">'
        + "".join(parts)
        + f'</div>'
    )

_SECTION_LEGEND = (
    '<div style="display:flex;gap:28px;align-items:center;margin-bottom:28px;">'
    + "".join(
        f'<span style="display:flex;align-items:center;gap:5px;">'
        f'<span style="width:4px;height:4px;border-radius:50%;background:{color};display:inline-block;flex-shrink:0;"></span>'
        f'<span style="font-size:11px;color:#888;">{label}</span>'
        f'</span>'
        for color, label in [
            ("#11F185", "Positive open to close"),
            ("#FF3D54", "Negative open to close"),
            ("#B71AFF", "Open to low"),
        ]
    )
    + '</div>'
)

def _notable_item(title: str, d: datetime.date, oc: float | None, ol: float | None = None) -> str:
    return (
        f'<div style="margin-bottom:22px;">'
        f'<span style="font-size:12px;color:#444;display:block;margin-bottom:0px;">{title}</span>'
        f'{_pct_spans(oc, ol)}'
        f'{_pill(d)}'
        f'</div>'
    )

with st.container(border=True):
    _kd_events = get_financial_events(_EVENT_IMPACT_START, datetime.date.today())

    # Group by normalised event type, preserving insertion order.
    _kd_grouped: dict[str, list[datetime.date]] = {}
    for _d, _lbl in _kd_events:
        _key = "OPEX" if "OPEX" in _lbl else _lbl
        _kd_grouped.setdefault(_key, []).append(_d)

    _kd_labels = {
        "OPEX": "OPEX (3rd Friday)",
        "VIX Exp": "VIX Expiration",
        "FOMC": "FOMC Day",
        "Thanksgiving": "Thanksgiving",
        "Xmas": "Christmas",
        "NYE": "New Year's Eve",
    }

    # --- OPEX / VIX Exp / FOMC in year columns ---
    main_html = ""
    for _key in ["OPEX", "VIX Exp", "FOMC"]:
        dates_for_key = sorted(_kd_grouped.get(_key, []), reverse=True)
        if not dates_for_key:
            continue
        by_year: dict[int, list[datetime.date]] = {}
        for d in dates_for_key:
            by_year.setdefault(d.year, []).append(d)
        year_cols = "".join(
            f'<div style="min-width:140px;">'
            + "".join(_pill(d) for d in sorted(by_year[yr]))
            + f'</div>'
            for yr in sorted(by_year.keys(), reverse=True)
        )
        main_html += (
            f'<div style="margin-bottom:24px;">'
            f'<p style="font-size:12px;font-weight:600;color:#1A1A1A;margin:0 0 10px 0;">'
            f'{_kd_labels[_key]}</p>'
            f'<div style="display:flex;flex-wrap:wrap;gap:24px;">{year_cols}</div>'
            f'</div>'
        )

    # --- Thanksgiving / Christmas / NYE as three side-by-side columns in one row ---
    holiday_cols = ""
    for _key in ["Thanksgiving", "Xmas", "NYE"]:
        dates_for_key = sorted(_kd_grouped.get(_key, []), reverse=True)
        if not dates_for_key:
            continue
        pills = "".join(_pill(d) for d in dates_for_key)
        holiday_cols += (
            f'<div style="min-width:140px;">'
            f'<p style="font-size:12px;font-weight:600;color:#1A1A1A;margin:0 0 10px 0;">'
            f'{_kd_labels[_key]}</p>'
            f'{pills}'
            f'</div>'
        )
    main_html += (
        f'<div style="margin-bottom:24px;">'
        f'<div style="display:flex;flex-wrap:wrap;gap:24px;">{holiday_cols}</div>'
        f'</div>'
    )

    st.markdown(
        f'<div style="padding:16px 0 4px 16px;width:fit-content;">{main_html}</div>',
        unsafe_allow_html=True,
    )

# =========================
# 6. NOTABLE EVENTS
# =========================
st.write("")
st.markdown('<div class="section-label">Notable events</div>', unsafe_allow_html=True)

with st.container(border=True):
    # Build open-to-close and open-to-low % lookups from the cached daily OHLC data.
    _ne_daily = _get_event_daily_df()
    _oc_pct: dict[datetime.date, float] = {}
    _ol_pct: dict[datetime.date, float] = {}
    if not _ne_daily.empty and {"Open", "Close", "Low"}.issubset(_ne_daily.columns):
        for _ts, _row in _ne_daily.iterrows():
            if _row["Open"] and _row["Open"] != 0:
                _d = _ts.date()
                _oc_pct[_d] = (_row["Close"] - _row["Open"]) / _row["Open"] * 100
                _ol_pct[_d] = (_row["Low"]   - _row["Open"]) / _row["Open"] * 100

    _notable_by_year: dict[int, list[tuple[datetime.date, str]]] = {}
    for _d, _label in _NOTABLE_EVENTS:
        _notable_by_year.setdefault(_d.year, []).append((_d, _label))

    _current_year = datetime.date.today().year
    _ne_min_year = min(_notable_by_year.keys()) if _notable_by_year else _current_year
    _ne_years = sorted(range(_ne_min_year, _current_year + 1), reverse=True)

    notable_year_cols = "".join(
        f'<div style="min-width:140px;">'
        f'<p style="font-size:12px;font-weight:600;color:#1A1A1A;margin:0 0 10px 0;">{yr}</p>'
        + "".join(
            _notable_item(_lbl, _d, _oc_pct.get(_d), _ol_pct.get(_d))
            for _d, _lbl in sorted(_notable_by_year.get(yr, []))
        )
        + f'</div>'
        for yr in _ne_years
    )
    st.markdown(
        f'<div style="padding:16px 0 4px 16px;width:fit-content;">'
        f'{_SECTION_LEGEND}'
        f'<div style="display:flex;flex-wrap:wrap;gap:24px;">{notable_year_cols}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

# =========================
# 7. ±1.5% INTRADAY MOVES
# =========================
st.write("")
st.markdown('<div class="section-label">Intraday moves ±1.5%</div>', unsafe_allow_html=True)

with st.container(border=True):
    _big_moves_df = _get_event_daily_df()

    if _big_moves_df.empty:
        st.caption("No daily data available.")
    else:
        # Compute open-to-close and open-to-low % change, filter to ≥1.5% moves since 2019.
        _bm = _big_moves_df[["Open", "High", "Low", "Close"]].copy()
        _bm["chg"]     = (_bm["Close"] - _bm["Open"]) / _bm["Open"] * 100
        _bm["chg_low"] = (_bm["Low"]   - _bm["Open"]) / _bm["Open"] * 100
        _bm["date"] = _bm.index.date
        _bm["year"] = _bm.index.year

        _bm_filtered = _bm[
            (_bm["year"] >= 2019) &
            (_bm["chg"].abs() >= 1.5)
        ].copy()

        if _bm_filtered.empty:
            st.caption("No ±1.5% days found.")
        else:
            def _bm_item(d: datetime.date, chg: float, chg_low: float) -> str:
                note = _INTRADAY_NOTES.get(d.strftime("%Y-%m-%d"))
                if note:
                    lines = note.split("\n", 1)
                    tag_html  = f'<span style="font-weight:600;opacity:0.7;display:block;margin-bottom:3px;">{lines[0]}</span>' if lines else ""
                    body_html = f'<span style="display:block;">{lines[1]}</span>' if len(lines) > 1 else ""
                    note_html = (
                        f'<span class="bm-note-wrap">'
                        f'<span class="bm-note-dot"></span>'
                        f'<span class="bm-note-tip">{tag_html}{body_html}</span>'
                        f'</span>'
                    )
                else:
                    note_html = ""
                return (
                    f'<div style="margin-bottom:22px;">'
                    f'{_pct_spans(chg, chg_low)}'
                    f'<div style="display:flex;align-items:center;gap:0;">'
                    f'{_pill(d)}{note_html}'
                    f'</div>'
                    f'</div>'
                )

            _bm_by_year: dict[int, list[tuple[datetime.date, float, float]]] = {}
            for _, row in _bm_filtered.iterrows():
                _bm_by_year.setdefault(int(row["year"]), []).append(
                    (row["date"], row["chg"], row["chg_low"])
                )

            bm_year_cols = "".join(
                f'<div style="min-width:140px;">'
                f'<p style="font-size:12px;font-weight:600;color:#1A1A1A;margin:0 0 10px 0;">{yr}</p>'
                + "".join(
                    _bm_item(d, chg, chg_low)
                    for d, chg, chg_low in sorted(_bm_by_year[yr])
                )
                + f'</div>'
                for yr in sorted(_bm_by_year.keys(), reverse=True)
            )

            st.markdown(
                f'<div style="padding:16px 0 4px 16px;width:fit-content;">'
                f'{_SECTION_LEGEND}'
                f'<div style="display:flex;flex-wrap:wrap;gap:24px;">{bm_year_cols}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
