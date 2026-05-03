"""Study page: long-term historical chart, lazy intraday explorer, day stats,
event-impact analysis, and two-date comparison overlay."""
from __future__ import annotations

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

# FirstRateData (SPX index — firstratedata.com, full purchase).
# 5-min coverage: 2008-01-02 → present (Schwab auto-archive fills forward).
# Daily coverage: 2000-11-27 → present.
_FRD_MIN_DATE  = datetime.date(2008, 1, 2)
_FRD_5MIN_PATH = os.path.join(_DATA_DIR, "SPX_5min.csv")
_FRD_1DAY_PATH = os.path.join(_DATA_DIR, "SPX_1day.csv")


@st.cache_data(ttl=None, show_spinner=False)
def _load_frd_5min() -> pd.DataFrame:
    """Load the FirstRateData 5-min CSV (SPX_5min.csv). Cached for the session."""
    if not os.path.exists(_FRD_5MIN_PATH):
        return pd.DataFrame()
    df = pd.read_csv(_FRD_5MIN_PATH, parse_dates=["timestamp"])
    df.set_index("timestamp", inplace=True)
    df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
    df.index = df.index.tz_localize(None)
    return df


@st.cache_data(ttl=None, show_spinner=False)
def _load_frd_daily() -> pd.DataFrame:
    """Load the FirstRateData daily CSV (SPX_1day.csv). Cached for the session."""
    if not os.path.exists(_FRD_1DAY_PATH):
        return pd.DataFrame()
    df = pd.read_csv(_FRD_1DAY_PATH, parse_dates=["date"])
    df.set_index("date", inplace=True)
    df.index = df.index.normalize()
    df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}, inplace=True)
    return df[["Open", "High", "Low", "Close"]].sort_index()


def _append_to_archive(df: pd.DataFrame) -> None:
    """Append freshly-fetched Schwab bars to SPX_5min.csv so they survive the 9-month window.

    Columns expected: Open, High, Low, Close (index = datetime, tz-naive ET).
    Writes lowercase column names (open/high/low/close) to match the CSV format
    that _load_frd_5min() expects. No-op if df is empty.
    """
    if df.empty:
        return

    archive_path = _FRD_5MIN_PATH

    # Rename to lowercase for the CSV format
    df_out = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
    df_out.index.name = "timestamp"

    if os.path.exists(archive_path):
        existing = pd.read_csv(archive_path, parse_dates=["timestamp"])
        existing.set_index("timestamp", inplace=True)
        existing.index = existing.index.tz_localize(None)
        combined = pd.concat([existing, df_out])
        combined = combined[~combined.index.duplicated(keep="first")]
        combined.sort_index(inplace=True)
        combined.to_csv(archive_path)
    else:
        df_out.to_csv(archive_path)


# --- LONG-TERM DAILY FETCHER ---
_FRD_DAILY_START = datetime.date(2000, 11, 27)  # earliest date in SPX_1day.csv

@st.cache_data(ttl=86400, show_spinner=False)
def get_spx_daily(years: int | None) -> pd.DataFrame:
    """Fetch daily SPX candles.
    Pass years=None for the full FRD history (back to Nov 2000).
    Priority: 1) Schwab API  2) FRD daily CSV"""
    now_ms = int(time.time() * 1000)
    if years is None:
        start_ms = int(datetime.datetime(_FRD_DAILY_START.year, _FRD_DAILY_START.month, _FRD_DAILY_START.day).timestamp() * 1000)
    else:
        start_ms = now_ms - 86400 * 1000 * 365 * years

    # 1. Schwab API
    raw = schwab_client.fetch_price_history(
        symbol="$SPX", period_type="year", freq_type="daily", freq=1,
        start_date=start_ms, end_date=now_ms,
    )
    if raw and 'candles' in raw:
        df = pd.DataFrame(raw['candles'])
        if not df.empty:
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

    # 2. FRD daily CSV (fallback if Schwab is unavailable)
    frd = _load_frd_daily()
    if not frd.empty:
        if years is None:
            return frd[frd.index >= pd.Timestamp(_FRD_DAILY_START)]
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        return frd[frd.index >= cutoff]

    return pd.DataFrame()


# Rolling 15-year window for event impact stats (uses FRD daily which starts Nov 2000).
_EVENT_IMPACT_YEARS = 15

@st.cache_data(ttl=86400, show_spinner=False)
def _get_event_daily_df() -> pd.DataFrame:
    """Daily SPX OHLC for the last 15 years.
    Priority: 1) Schwab API  2) FRD daily CSV"""
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - 86400 * 1000 * 365 * _EVENT_IMPACT_YEARS

    # 1. Schwab API (most accurate — live, rolling full history)
    raw = schwab_client.fetch_price_history(
        symbol="$SPX", period_type="year", freq_type="daily", freq=1,
        start_date=start_ms, end_date=now_ms,
    )
    if raw and 'candles' in raw:
        df = pd.DataFrame(raw['candles'])
        if not df.empty:
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

    # 2. FRD daily CSV (exact SPX index, full history)
    frd = _load_frd_daily()
    if not frd.empty:
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=_EVENT_IMPACT_YEARS)
        return frd[frd.index >= cutoff]

    return pd.DataFrame()
@st.cache_data(ttl=None, show_spinner="Loading 5-minute candles…")
def get_spx_5min_for_date(d: datetime.date) -> pd.DataFrame:
    # 1. SPX_5min.csv — exact SPX index (TwelveData or FirstRateData full purchase).
    #    Covers whatever date range was fetched; takes priority over approximations.
    frd = _load_frd_5min()
    if not frd.empty:
        day_df = frd[frd.index.date == d]
        if not day_df.empty:
            return day_df[["Open", "High", "Low", "Close"]]

    # 2. Schwab API (rolling ~9-month window for recent dates).
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
        if not df.empty:
            df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
            df['datetime'] = (
                df['datetime']
                .dt.tz_localize('UTC')
                .dt.tz_convert('America/New_York')
                .dt.tz_localize(None)
            )
            df = df[df['datetime'].dt.date == d]
            if not df.empty:
                df.set_index('datetime', inplace=True)
                df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
                _append_to_archive(df)  # persist before Schwab's 9-month window moves on
                return df

    return pd.DataFrame()


# ─── CONDITIONAL COMPARISON HELPERS ─────────────────────────────────────────

_CC_SNAP_TIMES = [(10, 0), (10, 30), (11, 0), (11, 30), (12, 0), (13, 0), (14, 0), (15, 0)]
_CC_TIME_OPTS  = [f"{h}:{m:02d}" for h, m in _CC_SNAP_TIMES]
_CC_COND_TYPES = [
    "% from open at time",
    "Days from event",
    "Day of week",
    "Month",
    "Overnight gap",
]
_CC_EVENT_OPTS = ["OPEX", "VIX Exp", "FOMC"]
_CC_DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
_CC_MON_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@st.cache_data(ttl=None, show_spinner="Building historical snapshot table…")
def _build_daily_snapshots() -> pd.DataFrame:
    """Precompute per-day metrics for the conditional comparison tool.

    Returns a DataFrame indexed by date (datetime.date) with columns:
      eod_pct, eod_low_pct, gap_pct,
      pct_at_HHMM / range_at_HHMM  (one pair per _CC_SNAP_TIMES entry),
      days_from_opex, days_from_vix_exp, days_from_fomc,
      day_of_week (0=Mon), month (1–12).
    """
    frd5 = _load_frd_5min()
    frd1 = _load_frd_daily()
    if frd5.empty:
        return pd.DataFrame()

    df = frd5.copy()
    df["_date"] = df.index.date
    df["_time"] = df.index.time

    grp_open  = df.groupby("_date")["Open"].first()
    grp_close = df.groupby("_date")["Close"].last()
    grp_low   = df.groupby("_date")["Low"].min()

    snap = pd.DataFrame(index=grp_open.index)
    snap.index.name = "date"
    snap["eod_pct"]     = (grp_close / grp_open - 1) * 100
    snap["eod_low_pct"] = (grp_low   / grp_open - 1) * 100

    # Overnight gap: today's open vs yesterday's close (from daily OHLC)
    if not frd1.empty:
        ds    = frd1.sort_index()
        gap_s = (ds["Open"] / ds["Close"].shift(1) - 1) * 100
        gap_s.index = gap_s.index.date
        snap["gap_pct"] = gap_s.reindex(snap.index)
    else:
        snap["gap_pct"] = np.nan

    # Intraday % from open and high-low range at each snapshot time
    for h, m in _CC_SNAP_TIMES:
        k  = f"{h:02d}{m:02d}"
        t  = datetime.time(h, m)
        sb = df[df["_time"] <= t]
        if sb.empty:
            snap[f"pct_at_{k}"]   = np.nan
            snap[f"range_at_{k}"] = np.nan
            continue
        sc = sb.groupby("_date")["Close"].last()
        sh = sb.groupby("_date")["High"].max()
        sl = sb.groupby("_date")["Low"].min()
        snap[f"pct_at_{k}"]   = ((sc / grp_open - 1) * 100).reindex(snap.index)
        snap[f"range_at_{k}"] = ((sh - sl) / grp_open * 100).reindex(snap.index)

    # Event proximity: signed calendar days from nearest event
    # negative = before event, positive = after event
    all_ds = sorted(snap.index.tolist())
    if all_ds:
        ev_s     = min(all_ds) - datetime.timedelta(days=90)
        ev_e     = max(all_ds) + datetime.timedelta(days=90)
        all_ev   = get_financial_events(ev_s, ev_e)
        opex_ds  = sorted(d for d, lbl in all_ev if "OPEX"    in lbl)
        vix_ds   = sorted(d for d, lbl in all_ev if lbl == "VIX Exp")
        fomc_ds  = sorted(d for d in FOMC_DATES   if ev_s <= d <= ev_e)

        def _near(d: datetime.date, evts: list) -> float:
            return float(min(((d - e).days for e in evts), key=abs)) if evts else np.nan

        snap["days_from_opex"]    = [_near(d, opex_ds) for d in snap.index]
        snap["days_from_vix_exp"] = [_near(d, vix_ds)  for d in snap.index]
        snap["days_from_fomc"]    = [_near(d, fomc_ds) for d in snap.index]
    else:
        snap["days_from_opex"] = snap["days_from_vix_exp"] = snap["days_from_fomc"] = np.nan

    snap["day_of_week"] = [d.weekday() for d in snap.index]
    snap["month"]       = [d.month     for d in snap.index]
    return snap


def _apply_cc_conditions(snap: pd.DataFrame) -> pd.DataFrame:
    """Filter the snapshot table against active conditions in session state."""
    mask = pd.Series(True, index=snap.index)
    for cid in st.session_state.get("cc_ids", []):
        if not st.session_state.get(f"cc_{cid}_enabled", True):
            continue
        ct = st.session_state.get(f"cc_{cid}_type", _CC_COND_TYPES[0])

        if ct == "% from open at time":
            col = "pct_at_" + st.session_state.get(f"cc_{cid}_time", "11:00").replace(":", "")
            lo  = float(st.session_state.get(f"cc_{cid}_pct_min", -1.0))
            hi  = float(st.session_state.get(f"cc_{cid}_pct_max", -0.1))
            if col in snap.columns:
                mask &= snap[col].between(lo, hi)

        elif ct == "Days from event":
            ev_map = {
                "OPEX":    "days_from_opex",
                "VIX Exp": "days_from_vix_exp",
                "FOMC":    "days_from_fomc",
            }
            col = ev_map.get(st.session_state.get(f"cc_{cid}_event", "VIX Exp"), "")
            lo  = int(st.session_state.get(f"cc_{cid}_days_min", -3))
            hi  = int(st.session_state.get(f"cc_{cid}_days_max",  3))
            if col and col in snap.columns:
                mask &= snap[col].between(lo, hi)

        elif ct == "Day of week":
            dows = st.session_state.get(f"cc_{cid}_dow", list(range(5)))
            if dows:
                mask &= snap["day_of_week"].isin(dows)

        elif ct == "Month":
            mos = st.session_state.get(f"cc_{cid}_months", list(range(1, 13)))
            if mos:
                mask &= snap["month"].isin(mos)

        elif ct == "Overnight gap":
            lo = float(st.session_state.get(f"cc_{cid}_gap_min", -1.0))
            hi = float(st.session_state.get(f"cc_{cid}_gap_max",  1.0))
            if "gap_pct" in snap.columns:
                mask &= snap["gap_pct"].between(lo, hi)

    return snap[mask]


# =========================
# 1. LONG-TERM CHART
# =========================
st.markdown('<div class="section-label">Long-term chart</div>', unsafe_allow_html=True)
with st.container(border=True):
    range_params = {"1Y": 1, "2Y": 2, "5Y": 5, "10Y": 10, "Max": None}

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
frd_loaded = os.path.exists(_FRD_5MIN_PATH)
min_date = _FRD_MIN_DATE if frd_loaded else today - datetime.timedelta(days=MINUTE_HISTORY_DAYS)

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

                # 2. Fall back to the FRD 5-min CSV (already cached in memory).
                if prior_close is None:
                    _src = _load_frd_5min()
                    if not _src.empty:
                        _prior_bars = _src[_src.index.date < selected_date]
                        if not _prior_bars.empty:
                            prior_close = float(_prior_bars['Close'].iloc[-1])

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
st.markdown('<div class="section-label">Event comparison</div>', unsafe_allow_html=True)

_CMP_COLORS = [
    "#B71AFF", "#4B7BFF", "#FF6B35", "#11B8A0",
    "#FF3D54", "#F5A623", "#4CAF50", "#888888",
]
_CMP_ENTRY_TYPES = ["FOMC", "OPEX", "VIX Exp", "Specific date"]
_CMP_OFFSET_OPTS = ["-3 days", "-2 days", "-1 day", "Day of", "+1 day", "+2 days"]
_CMP_OFFSET_VALS = {"-3 days": -3, "-2 days": -2, "-1 day": -1,
                    "Day of": 0, "+1 day": 1, "+2 days": 2}
_CMP_RANGE_OPTS  = ["3M", "6M", "1Y", "2Y", "All"]
_CMP_RANGE_DAYS  = {"3M": 91, "6M": 182, "1Y": 365, "2Y": 730, "All": None}
_CMP_GAP_OPTS    = ["All", "Gap up ↑", "Gap down ↓"]

# ── Session state init ─────────────────────────────────────────────────────
if "cmp_ids"     not in st.session_state: st.session_state["cmp_ids"]     = []
if "cmp_next_id" not in st.session_state: st.session_state["cmp_next_id"] = 0
if "cmp_range"   not in st.session_state: st.session_state["cmp_range"]   = "All"
if "cmp_gap"     not in st.session_state: st.session_state["cmp_gap"]     = "All"


def _cmp_add(entry_type: str = "FOMC", offset: str = "Day of") -> None:
    cid = st.session_state["cmp_next_id"]
    st.session_state["cmp_next_id"] += 1
    st.session_state["cmp_ids"].append(cid)
    st.session_state[f"cmp_{cid}_type"]    = entry_type
    st.session_state[f"cmp_{cid}_offset"]  = offset
    st.session_state[f"cmp_{cid}_date"]    = datetime.date.today() - datetime.timedelta(days=1)
    st.session_state[f"cmp_{cid}_enabled"] = True


def _cmp_del(cid: int) -> None:
    st.session_state["cmp_ids"].remove(cid)


# Seed default entry on first load
if not st.session_state["cmp_ids"]:
    _cmp_add("FOMC", "Day of")


with st.container(border=True):
    today = datetime.date.today()
    frd_loaded = os.path.exists(_FRD_5MIN_PATH)
    min_date = _FRD_MIN_DATE if frd_loaded else today - datetime.timedelta(days=MINUTE_HISTORY_DAYS)

    # ── Header ─────────────────────────────────────────────────────────────
    _cmp_h1, _cmp_h2 = st.columns([2, 8])
    with _cmp_h1:
        if st.button("＋  Add event", key="cmp_add_btn", use_container_width=True):
            _cmp_add()
    if st.session_state["cmp_ids"]:
        with _cmp_h2:
            _, _cmp_clr = st.columns([9, 1])
            with _cmp_clr:
                if st.button("Clear all", key="cmp_clr_btn"):
                    st.session_state["cmp_ids"] = []

    # ── Entry rows ─────────────────────────────────────────────────────────
    for _ci, _cid in enumerate(list(st.session_state["cmp_ids"])):
        st.markdown(
            '<hr style="border:none;border-top:1px solid #EBEBEB;margin:6px 0 4px;">',
            unsafe_allow_html=True,
        )
        _cmp_dot = _CMP_COLORS[_ci % len(_CMP_COLORS)]
        _ct_col, _co_col, _cd_col, _ctog, _cdel = st.columns([1.8, 1.8, 2.2, 0.55, 0.45])

        with _ct_col:
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:7px;margin-bottom:2px;">'
                f'<span style="width:8px;height:8px;border-radius:50%;background:{_cmp_dot};'
                f'display:inline-block;flex-shrink:0;"></span>'
                f'<span style="font-size:11px;color:#999;">Event type</span></div>',
                unsafe_allow_html=True,
            )
            st.selectbox(
                "Type", _CMP_ENTRY_TYPES,
                key=f"cmp_{_cid}_type",
                label_visibility="collapsed",
            )
        _cmp_ct = st.session_state[f"cmp_{_cid}_type"]

        with _co_col:
            if _cmp_ct != "Specific date":
                st.markdown(
                    '<p style="font-size:11px;color:#999;margin-bottom:2px;">Offset</p>',
                    unsafe_allow_html=True,
                )
                st.selectbox(
                    "Offset", _CMP_OFFSET_OPTS,
                    key=f"cmp_{_cid}_offset",
                    label_visibility="collapsed",
                )
            else:
                st.write("")

        with _cd_col:
            if _cmp_ct == "Specific date":
                st.markdown(
                    '<p style="font-size:11px;color:#999;margin-bottom:2px;">Date</p>',
                    unsafe_allow_html=True,
                )
                st.date_input(
                    "Date",
                    min_value=min_date,
                    max_value=today,
                    format="MM/DD/YYYY",
                    key=f"cmp_{_cid}_date",
                    label_visibility="collapsed",
                )
            else:
                st.write("")

        with _ctog:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">&nbsp;</p>',
                unsafe_allow_html=True,
            )
            st.toggle("On", key=f"cmp_{_cid}_enabled", label_visibility="collapsed")

        with _cdel:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">&nbsp;</p>',
                unsafe_allow_html=True,
            )
            if st.button("✕", key=f"cmp_del_{_cid}"):
                _cmp_del(_cid)
                st.rerun()

    # ── Global filters ──────────────────────────────────────────────────────
    if st.session_state["cmp_ids"]:
        st.markdown('<div style="height:6px;"></div>', unsafe_allow_html=True)
        _cf1, _cf2 = st.columns(2)
        with _cf1:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">Time range</p>',
                unsafe_allow_html=True,
            )
            st.pills(
                "Range", _CMP_RANGE_OPTS,
                key="cmp_range",
                label_visibility="collapsed",
            )
        with _cf2:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">Overnight gap</p>',
                unsafe_allow_html=True,
            )
            st.pills(
                "Gap", _CMP_GAP_OPTS,
                key="cmp_gap",
                label_visibility="collapsed",
            )

    st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)

    # ── Build gap map and trading day index from daily data ─────────────────
    _ref = datetime.date(2000, 1, 3)
    _cmp_daily = _load_frd_daily()
    _cmp_gap_map: dict[datetime.date, float] = {}
    _cmp_td_idx: pd.DatetimeIndex = pd.DatetimeIndex([])
    if not _cmp_daily.empty:
        _cmp_ds = _cmp_daily.sort_index()
        _cmp_td_idx = _cmp_ds.index
        _cmp_gap_s = (_cmp_ds["Open"] / _cmp_ds["Close"].shift(1) - 1) * 100
        for _ts, _gv in _cmp_gap_s.items():
            if pd.notna(_gv):
                _cmp_gap_map[_ts.date()] = float(_gv)

    # Pre-load the full 5-min CSV once — slicing it per date is much faster
    # than calling get_spx_5min_for_date() which may trigger Schwab API calls.
    _cmp_frd5 = _load_frd_5min()

    def _cmp_day_bars(d: datetime.date) -> pd.DataFrame:
        """Return 5-min bars for date, reading CSV directly without API calls."""
        if not _cmp_frd5.empty:
            _ots = pd.Timestamp(d)
            _ote = _ots + pd.Timedelta(hours=23, minutes=59)
            _day = _cmp_frd5.loc[_ots:_ote]
            if not _day.empty:
                return _day[["Open", "High", "Low", "Close"]]
        # Only touch Schwab for recent dates that may not yet be in the CSV
        if d >= datetime.date.today() - datetime.timedelta(days=MINUTE_HISTORY_DAYS):
            return get_spx_5min_for_date(d)
        return pd.DataFrame()

    def _to_time_axis(df: pd.DataFrame):
        if df.empty:
            return [], []
        times = [datetime.datetime.combine(_ref, ts.time()) for ts in df.index]
        open_px = float(df["Open"].iloc[0])
        pct = ((df["Close"] / open_px - 1) * 100).round(2)
        return times, pct

    def _cmp_resolve(cid: int) -> tuple[list[datetime.date], str]:
        """Resolve one entry to (date_list, legend_label)."""
        ctype   = st.session_state.get(f"cmp_{cid}_type", "FOMC")
        today_  = datetime.date.today()

        if ctype == "Specific date":
            d = st.session_state.get(f"cmp_{cid}_date")
            if isinstance(d, datetime.date):
                return [d], d.strftime("%b %-d, %Y")
            return [], "Specific date"

        # Time range cutoff
        rng      = st.session_state.get("cmp_range") or "1Y"
        rng_days = _CMP_RANGE_DAYS.get(rng)
        cutoff   = (today_ - datetime.timedelta(days=rng_days)) if rng_days else datetime.date(2000, 1, 1)

        # Raw event dates within the window
        all_ev = get_financial_events(cutoff, today_)
        if ctype == "FOMC":
            raw = sorted(d for d in FOMC_DATES if cutoff <= d <= today_)
        elif ctype == "OPEX":
            raw = sorted(d for d, lbl in all_ev if "OPEX" in lbl and d <= today_)
        else:  # VIX Exp
            raw = sorted(d for d, lbl in all_ev if lbl == "VIX Exp" and d <= today_)

        # Apply trading-day offset using the daily calendar index
        offset_val = _CMP_OFFSET_VALS.get(
            st.session_state.get(f"cmp_{cid}_offset", "Day of"), 0
        )
        resolved = []
        if len(_cmp_td_idx) > 0:
            for ev_d in raw:
                pos    = int(_cmp_td_idx.searchsorted(pd.Timestamp(ev_d)))
                target = pos + offset_val
                if 0 <= target < len(_cmp_td_idx):
                    resolved.append(_cmp_td_idx[target].date())

        # Overnight gap filter
        gap_f = st.session_state.get("cmp_gap") or "All"
        if gap_f == "Gap up ↑":
            resolved = [d for d in resolved if _cmp_gap_map.get(d, 0) > 0]
        elif gap_f == "Gap down ↓":
            resolved = [d for d in resolved if _cmp_gap_map.get(d, 0) < 0]

        # Deduplicate preserving order
        seen, out = set(), []
        for d in resolved:
            if d not in seen:
                seen.add(d)
                out.append(d)

        off = st.session_state.get(f"cmp_{cid}_offset", "Day of")
        off_label = f" ({off})" if off != "Day of" else ""
        return out, f"{ctype}{off_label}"

    # ── Resolve all entries once, reuse for both histogram and chart ─────────
    _cmp_fig = go.Figure()
    _cmp_legend_items: list[tuple[str, str, int]] = []
    _cmp_all_entries: list[tuple[str, str, list[datetime.date]]] = []  # (color, label, dates)

    for _ci, _cid in enumerate(st.session_state["cmp_ids"]):
        if not st.session_state.get(f"cmp_{_cid}_enabled", True):
            continue
        _cmp_color = _CMP_COLORS[_ci % len(_CMP_COLORS)]
        _cmp_dates, _cmp_lbl = _cmp_resolve(_cid)
        if not _cmp_dates:
            continue
        _cmp_legend_items.append((_cmp_color, _cmp_lbl, len(_cmp_dates)))
        _cmp_all_entries.append((_cmp_color, _cmp_lbl, _cmp_dates))

    # ── Histogram (EOD returns from daily OHLC — no 5-min needed) ────────────
    if _cmp_all_entries and not _cmp_daily.empty:
        _cmp_eod_all: list[float] = []
        for _, _, _entry_dates in _cmp_all_entries:
            for _ed in _entry_dates:
                _ed_ts = pd.Timestamp(_ed)
                if _ed_ts in _cmp_daily.index:
                    _row = _cmp_daily.loc[_ed_ts]
                    if _row["Open"] != 0:
                        _cmp_eod_all.append(
                            float((_row["Close"] - _row["Open"]) / _row["Open"] * 100)
                        )

        if _cmp_eod_all:
            _cmp_eod_s  = pd.Series(_cmp_eod_all)
            _cmp_h_mean = _cmp_eod_s.mean()
            _cmp_h_med  = _cmp_eod_s.median()
            _cmp_h_ppos = (_cmp_eod_s >= 0).mean() * 100
            _cmp_h_std  = _cmp_eod_s.std()
            _cmp_h_n    = len(_cmp_eod_s)

            # N badge
            if _cmp_h_n < 30:
                _hbg, _hfg = "#FF8C0020", "#CC7000"
            elif _cmp_h_n < 75:
                _hbg, _hfg = "#F5C51820", "#A08500"
            else:
                _hbg, _hfg = "#11F18520", "#0AA855"

            def _cmp_spill(label: str, val: str, color: str = "#444") -> str:
                return (
                    f'<span style="display:inline-block;padding:4px 12px;border-radius:6px;'
                    f'background:#F1F2F6;font-size:12px;color:#555;margin:0 6px 6px 0;">'
                    f'{label}: <b style="color:{color};">{val}</b></span>'
                )

            _cmp_mc = "#11F185" if _cmp_h_mean >= 0 else "#FF3D54"
            _cmp_dc = "#11F185" if _cmp_h_med  >= 0 else "#FF3D54"
            _cmp_pc = "#11F185" if _cmp_h_ppos >= 50 else "#FF3D54"

            st.markdown(
                f'<div style="display:inline-block;padding:4px 12px;border-radius:7px;'
                f'background:{_hbg};border:1px solid {_hfg}44;'
                f'font-size:12px;font-weight:600;color:{_hfg};margin-bottom:10px;">'
                f'N = {_cmp_h_n}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div style="margin-bottom:10px;">'
                + _cmp_spill("Mean EOD",   f'{"+" if _cmp_h_mean >= 0 else ""}{_cmp_h_mean:.2f}%', _cmp_mc)
                + _cmp_spill("Median EOD", f'{"+" if _cmp_h_med  >= 0 else ""}{_cmp_h_med:.2f}%',  _cmp_dc)
                + _cmp_spill("% Positive", f'{_cmp_h_ppos:.0f}%',                                   _cmp_pc)
                + _cmp_spill("Std Dev",    f'{_cmp_h_std:.2f}%')
                + '</div>',
                unsafe_allow_html=True,
            )

            _cmp_rng  = float(_cmp_eod_s.max() - _cmp_eod_s.min())
            _cmp_bsz  = 0.1 if _cmp_rng < 1.5 else (0.25 if _cmp_rng < 5.0 else 0.5)
            _cmp_blo  = np.floor(_cmp_eod_s.min() / _cmp_bsz) * _cmp_bsz - _cmp_bsz
            _cmp_bhi  = np.ceil( _cmp_eod_s.max() / _cmp_bsz) * _cmp_bsz + _cmp_bsz
            _cmp_bins = np.arange(_cmp_blo, _cmp_bhi + _cmp_bsz, _cmp_bsz)
            _cmp_cnts, _cmp_edges = np.histogram(_cmp_eod_s.values, bins=_cmp_bins)
            _cmp_ctrs  = (_cmp_edges[:-1] + _cmp_edges[1:]) / 2
            _cmp_bclrs = ["#11F185" if c >= 0 else "#FF3D54" for c in _cmp_ctrs]
            _cmp_bpcts = _cmp_cnts / _cmp_cnts.sum() * 100 if _cmp_cnts.sum() > 0 else _cmp_cnts * 0.0

            _cmp_hfig = go.Figure()
            _cmp_hfig.add_trace(go.Bar(
                x=_cmp_ctrs, y=_cmp_cnts,
                marker_color=_cmp_bclrs, marker_line_width=0,
                width=_cmp_bsz * 0.88,
                customdata=_cmp_bpcts,
                hovertemplate="%{x:+.2f}%  →  %{y} days (%{customdata:.1f}%)<extra></extra>",
            ))
            _cmp_hfig.add_vline(x=0, line_color="#C8C8C8", line_width=1, line_dash="dot")
            _cmp_hfig.add_vline(x=_cmp_h_mean, line_color="#1A1A1A", line_width=1.5)
            _cmp_hfig.add_vline(x=_cmp_h_med,  line_color="#888888", line_width=1, line_dash="dot")
            _cmp_hfig.add_annotation(
                x=_cmp_h_mean, xref="x", y=1.08, yref="paper",
                text=f"mean {_cmp_h_mean:+.2f}%",
                showarrow=False, xanchor="right", yanchor="bottom",
                font=dict(size=10, color="#1A1A1A"),
            )
            _cmp_hfig.add_annotation(
                x=_cmp_h_med, xref="x", y=1.08, yref="paper",
                text=f"median {_cmp_h_med:+.2f}%",
                showarrow=False, xanchor="left", yanchor="bottom",
                font=dict(size=10, color="#888888"),
            )
            _cmp_hfig.update_layout(
                height=260,
                margin=dict(l=50, r=20, t=46, b=40),
                plot_bgcolor="white", paper_bgcolor="white",
                bargap=0.06,
                xaxis=dict(
                    showgrid=True, gridcolor="#F0F0F0", ticksuffix="%",
                    title=dict(text="EOD % from open", font=dict(size=11, color="#888")),
                ),
                yaxis=dict(
                    showgrid=True, gridcolor="#F0F0F0",
                    title=dict(text="# of days", font=dict(size=11, color="#888")),
                ),
                showlegend=False,
            )
            st.plotly_chart(
                _cmp_hfig, use_container_width=True,
                key="cmp_hist", config={"displayModeBar": False},
            )

    # ── Build intraday overlay chart ─────────────────────────────────────────
    for _cmp_color, _cmp_lbl, _cmp_dates in _cmp_all_entries:
        for _cd in _cmp_dates:
            _cdf = _cmp_day_bars(_cd)
            if _cdf.empty:
                continue
            _cx, _cy = _to_time_axis(_cdf)
            if not _cx:
                continue
            _cmp_fig.add_trace(go.Scatter(
                x=_cx, y=_cy, mode="lines",
                legendgroup=_cmp_lbl,
                showlegend=False,
                line=dict(color=_cmp_color, width=0.9),
                opacity=0.5,
                hovertemplate=f'{_cd.strftime("%b %-d, %Y")}: %{{y:+.2f}}%<extra></extra>',
            ))

    _cmp_fig.add_hline(y=0, line_dash="dot", line_color="#B2B2B2", line_width=1)
    _cmp_fig.update_layout(
        dragmode="zoom", uirevision="constant",
        height=560,
        margin=dict(l=60, r=20, t=10, b=30),
        plot_bgcolor="white", paper_bgcolor="white",
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="rgba(255, 255, 255, 0.85)",
            bordercolor="rgba(0, 0, 0, 0)",
            font=dict(color="#1E1E1E"),
        ),
        xaxis=dict(
            showgrid=True, gridcolor="#F0F0F0",
            tickformat="%H:%M", hoverformat="%H:%M",
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

    # Legend strip above chart
    if _cmp_legend_items:
        _leg_html = '<div style="display:flex;flex-wrap:wrap;gap:16px;margin-bottom:8px;">'
        for _lc, _ll, _ln in _cmp_legend_items:
            _leg_html += (
                f'<span style="display:flex;align-items:center;gap:5px;">'
                f'<span style="width:12px;height:3px;background:{_lc};border-radius:2px;'
                f'display:inline-block;"></span>'
                f'<span style="font-size:12px;color:#444;">{_ll}</span>'
                f'<span style="font-size:11px;color:#aaa;">({_ln})</span>'
                f'</span>'
            )
        _leg_html += '</div>'
        st.markdown(_leg_html, unsafe_allow_html=True)

    st.plotly_chart(
        _cmp_fig,
        use_container_width=True,
        key="study_compare_chart",
        config={'displayModeBar': False},
    )


# =========================
# 4. CONDITIONAL COMPARISON
# =========================
st.write("")
st.markdown('<div class="section-label">Conditional comparison</div>', unsafe_allow_html=True)

# Session state init
if "cc_ids"     not in st.session_state:
    st.session_state["cc_ids"]     = []
if "cc_next_id" not in st.session_state:
    st.session_state["cc_next_id"] = 0


def _cc_add() -> None:
    cid = st.session_state["cc_next_id"]
    st.session_state["cc_next_id"] += 1
    st.session_state["cc_ids"].append(cid)
    # Initialise all possible keys so widgets never see a missing key
    st.session_state[f"cc_{cid}_type"]     = _CC_COND_TYPES[0]
    st.session_state[f"cc_{cid}_enabled"]  = True
    st.session_state[f"cc_{cid}_time"]     = "11:00"
    st.session_state[f"cc_{cid}_pct_min"]  = -1.0
    st.session_state[f"cc_{cid}_pct_max"]  = -0.1
    st.session_state[f"cc_{cid}_event"]    = "VIX Exp"
    st.session_state[f"cc_{cid}_days_min"] = -3
    st.session_state[f"cc_{cid}_days_max"] =  3
    st.session_state[f"cc_{cid}_gap_min"]  = -1.0
    st.session_state[f"cc_{cid}_gap_max"]  =  1.0
    st.session_state[f"cc_{cid}_dow"]      = list(range(5))
    st.session_state[f"cc_{cid}_months"]   = list(range(1, 13))


def _cc_del(cid: int) -> None:
    st.session_state["cc_ids"].remove(cid)


with st.container(border=True):
    # ── Header row ────────────────────────────────────────────────────────
    _cc_hdr_l, _cc_hdr_r = st.columns([2, 8])
    with _cc_hdr_l:
        if st.button("＋  Add condition", key="cc_add_btn", use_container_width=True):
            _cc_add()
    if st.session_state["cc_ids"]:
        with _cc_hdr_r:
            _, _cc_clr_col = st.columns([9, 1])
            with _cc_clr_col:
                if st.button("Clear all", key="cc_clr_btn"):
                    st.session_state["cc_ids"] = []

    # ── Condition rows ─────────────────────────────────────────────────────
    for _cid in list(st.session_state["cc_ids"]):
        st.markdown(
            '<hr style="border:none;border-top:1px solid #EBEBEB;margin:6px 0 4px;">',
            unsafe_allow_html=True,
        )
        _cc_type_col, _cc_params_col, _cc_tog_col, _cc_del_col = st.columns(
            [2, 5, 0.55, 0.45]
        )

        with _cc_type_col:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">Condition type</p>',
                unsafe_allow_html=True,
            )
            st.selectbox(
                "Type", _CC_COND_TYPES,
                key=f"cc_{_cid}_type",
                label_visibility="collapsed",
            )
        _ct = st.session_state[f"cc_{_cid}_type"]

        with _cc_params_col:
            if _ct == "% from open at time":
                _pa, _pb, _pc = st.columns([1.2, 1, 1])
                with _pa:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">At time (ET)</p>',
                        unsafe_allow_html=True,
                    )
                    st.selectbox(
                        "Time", _CC_TIME_OPTS,
                        key=f"cc_{_cid}_time",
                        label_visibility="collapsed",
                    )
                with _pb:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Min %</p>',
                        unsafe_allow_html=True,
                    )
                    st.number_input(
                        "Min %", step=0.1, format="%.2f",
                        key=f"cc_{_cid}_pct_min",
                        label_visibility="collapsed",
                    )
                with _pc:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Max %</p>',
                        unsafe_allow_html=True,
                    )
                    st.number_input(
                        "Max %", step=0.1, format="%.2f",
                        key=f"cc_{_cid}_pct_max",
                        label_visibility="collapsed",
                    )

            elif _ct == "Days from event":
                _pa, _pb, _pc = st.columns([1.2, 1, 1])
                with _pa:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Event</p>',
                        unsafe_allow_html=True,
                    )
                    st.selectbox(
                        "Event", _CC_EVENT_OPTS,
                        key=f"cc_{_cid}_event",
                        label_visibility="collapsed",
                    )
                with _pb:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Min days (neg = before)</p>',
                        unsafe_allow_html=True,
                    )
                    st.number_input(
                        "Min days", step=1,
                        key=f"cc_{_cid}_days_min",
                        label_visibility="collapsed",
                    )
                with _pc:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Max days (pos = after)</p>',
                        unsafe_allow_html=True,
                    )
                    st.number_input(
                        "Max days", step=1,
                        key=f"cc_{_cid}_days_max",
                        label_visibility="collapsed",
                    )

            elif _ct == "Day of week":
                st.markdown(
                    '<p style="font-size:11px;color:#999;margin-bottom:2px;">Select days</p>',
                    unsafe_allow_html=True,
                )
                st.multiselect(
                    "Days", list(range(5)),
                    format_func=lambda x: _CC_DOW_LABELS[x],
                    key=f"cc_{_cid}_dow",
                    label_visibility="collapsed",
                )

            elif _ct == "Month":
                st.markdown(
                    '<p style="font-size:11px;color:#999;margin-bottom:2px;">Select months</p>',
                    unsafe_allow_html=True,
                )
                st.multiselect(
                    "Months", list(range(1, 13)),
                    format_func=lambda x: _CC_MON_LABELS[x - 1],
                    key=f"cc_{_cid}_months",
                    label_visibility="collapsed",
                )

            elif _ct == "Overnight gap":
                _pa, _pb = st.columns(2)
                with _pa:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Min gap %</p>',
                        unsafe_allow_html=True,
                    )
                    st.number_input(
                        "Min gap %", step=0.1, format="%.2f",
                        key=f"cc_{_cid}_gap_min",
                        label_visibility="collapsed",
                    )
                with _pb:
                    st.markdown(
                        '<p style="font-size:11px;color:#999;margin-bottom:2px;">Max gap %</p>',
                        unsafe_allow_html=True,
                    )
                    st.number_input(
                        "Max gap %", step=0.1, format="%.2f",
                        key=f"cc_{_cid}_gap_max",
                        label_visibility="collapsed",
                    )

        with _cc_tog_col:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">&nbsp;</p>',
                unsafe_allow_html=True,
            )
            st.toggle("On", key=f"cc_{_cid}_enabled", label_visibility="collapsed")

        with _cc_del_col:
            st.markdown(
                '<p style="font-size:11px;color:#999;margin-bottom:2px;">&nbsp;</p>',
                unsafe_allow_html=True,
            )
            if st.button("✕", key=f"cc_del_{_cid}"):
                _cc_del(_cid)
                st.rerun()

    st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)

    # ── Results ────────────────────────────────────────────────────────────
    _cc_snap = _build_daily_snapshots()

    if _cc_snap.empty:
        st.info("No 5-min historical data available for comparison.")
    elif not st.session_state["cc_ids"]:
        st.markdown(
            '<p style="font-size:13px;color:#aaa;padding:2px 0 8px;">'
            'Add a condition above to filter historical days and see EOD return distribution.</p>',
            unsafe_allow_html=True,
        )
    else:
        _cc_matched = _apply_cc_conditions(_cc_snap)
        _cc_n = len(_cc_matched)

        # N badge
        if _cc_n == 0:
            _cc_bg, _cc_fg, _cc_msg = "#FF3D5420", "#FF3D54", "No matching days"
        elif _cc_n < 30:
            _cc_bg, _cc_fg, _cc_msg = "#FF8C0020", "#CC7000", f"N = {_cc_n}  ·  thin sample — interpret carefully"
        elif _cc_n < 75:
            _cc_bg, _cc_fg, _cc_msg = "#F5C51820", "#A08500", f"N = {_cc_n}  ·  moderate sample"
        else:
            _cc_bg, _cc_fg, _cc_msg = "#11F18520", "#0AA855", f"N = {_cc_n}  ·  solid sample"

        st.markdown(
            f'<div style="display:inline-block;padding:5px 14px;border-radius:8px;'
            f'background:{_cc_bg};border:1px solid {_cc_fg}44;'
            f'font-size:13px;font-weight:600;color:{_cc_fg};margin-bottom:14px;">'
            f'{_cc_msg}</div>',
            unsafe_allow_html=True,
        )

        if _cc_n > 0:
            _cc_eod = _cc_matched["eod_pct"].dropna()

            if not _cc_eod.empty:
                _cc_mean   = _cc_eod.mean()
                _cc_med    = _cc_eod.median()
                _cc_ppos   = (_cc_eod >= 0).mean() * 100
                _cc_std    = _cc_eod.std()

                def _cc_pill(label: str, val: str, color: str = "#444") -> str:
                    return (
                        f'<span style="display:inline-block;padding:4px 12px;border-radius:6px;'
                        f'background:#F1F2F6;font-size:12px;color:#555;margin:0 6px 6px 0;">'
                        f'{label}: <b style="color:{color};">{val}</b></span>'
                    )

                _mc = "#11F185" if _cc_mean >= 0 else "#FF3D54"
                _dc = "#11F185" if _cc_med  >= 0 else "#FF3D54"
                _pc = "#11F185" if _cc_ppos >= 50 else "#FF3D54"
                st.markdown(
                    '<div style="margin-bottom:12px;">'
                    + _cc_pill("Mean EOD",    f'{"+" if _cc_mean >= 0 else ""}{_cc_mean:.2f}%', _mc)
                    + _cc_pill("Median EOD",  f'{"+" if _cc_med  >= 0 else ""}{_cc_med:.2f}%',  _dc)
                    + _cc_pill("% Positive",  f'{_cc_ppos:.0f}%',                               _pc)
                    + _cc_pill("Std Dev",     f'{_cc_std:.2f}%')
                    + '</div>',
                    unsafe_allow_html=True,
                )

                # Histogram — adaptive bin size
                _cc_range = float(_cc_eod.max() - _cc_eod.min())
                _cc_bsz   = 0.1 if _cc_range < 1.5 else (0.25 if _cc_range < 5.0 else 0.5)
                _cc_blo   = np.floor(_cc_eod.min() / _cc_bsz) * _cc_bsz - _cc_bsz
                _cc_bhi   = np.ceil( _cc_eod.max() / _cc_bsz) * _cc_bsz + _cc_bsz
                _cc_bins  = np.arange(_cc_blo, _cc_bhi + _cc_bsz, _cc_bsz)
                _cc_cnts, _cc_edges = np.histogram(_cc_eod.values, bins=_cc_bins)
                _cc_ctrs  = (_cc_edges[:-1] + _cc_edges[1:]) / 2
                _cc_bclrs = ["#11F185" if c >= 0 else "#FF3D54" for c in _cc_ctrs]

                _cc_bin_pcts = _cc_cnts / _cc_cnts.sum() * 100 if _cc_cnts.sum() > 0 else _cc_cnts * 0.0

                _cc_hfig = go.Figure()
                _cc_hfig.add_trace(go.Bar(
                    x=_cc_ctrs, y=_cc_cnts,
                    marker_color=_cc_bclrs,
                    marker_line_width=0,
                    width=_cc_bsz * 0.88,
                    customdata=_cc_bin_pcts,
                    hovertemplate="%{x:+.2f}%  →  %{y} days (%{customdata:.1f}%)<extra></extra>",
                ))
                _cc_hfig.add_vline(
                    x=0, line_color="#C8C8C8", line_width=1, line_dash="dot",
                )
                _cc_hfig.add_vline(x=_cc_mean, line_color="#1A1A1A", line_width=1.5)
                _cc_hfig.add_vline(x=_cc_med,  line_color="#888888", line_width=1, line_dash="dot")
                _cc_hfig.add_annotation(
                    x=_cc_mean, xref="x", y=1.08, yref="paper",
                    text=f"mean {_cc_mean:+.2f}%",
                    showarrow=False, xanchor="right", yanchor="bottom",
                    font=dict(size=10, color="#1A1A1A"),
                )
                _cc_hfig.add_annotation(
                    x=_cc_med, xref="x", y=1.08, yref="paper",
                    text=f"median {_cc_med:+.2f}%",
                    showarrow=False, xanchor="left", yanchor="bottom",
                    font=dict(size=10, color="#888888"),
                )
                _cc_hfig.update_layout(
                    height=300,
                    margin=dict(l=50, r=20, t=46, b=40),
                    plot_bgcolor="white", paper_bgcolor="white",
                    bargap=0.06,
                    xaxis=dict(
                        showgrid=True, gridcolor="#F0F0F0", ticksuffix="%",
                        title=dict(text="EOD % from open", font=dict(size=11, color="#888")),
                    ),
                    yaxis=dict(
                        showgrid=True, gridcolor="#F0F0F0",
                        title=dict(text="# of days", font=dict(size=11, color="#888")),
                    ),
                    showlegend=False,
                )
                st.plotly_chart(
                    _cc_hfig, use_container_width=True,
                    key="cc_hist", config={"displayModeBar": False},
                )

                # Intraday overlay
                if st.checkbox(
                    "Show intraday traces for matching days",
                    key="cc_overlay_tog",
                    value=True,
                ):
                    _cc_ov_dates = sorted(_cc_matched.index.tolist(), reverse=True)[:25]
                    _cc_frd5     = _load_frd_5min()
                    _cc_ref      = datetime.date(2000, 1, 3)
                    _cc_ofig     = go.Figure()

                    for _cc_od in _cc_ov_dates:
                        _cc_ots = pd.Timestamp(_cc_od)
                        _cc_ote = _cc_ots + pd.Timedelta(hours=23, minutes=59)
                        _cc_odf = (
                            _cc_frd5.loc[_cc_ots:_cc_ote]
                            if not _cc_frd5.empty else pd.DataFrame()
                        )
                        if _cc_odf.empty:
                            continue
                        _cc_ox   = [
                            datetime.datetime.combine(_cc_ref, ts.time())
                            for ts in _cc_odf.index
                        ]
                        _cc_oo   = float(_cc_odf["Open"].iloc[0])
                        _cc_oy   = ((_cc_odf["Close"] / _cc_oo - 1) * 100).round(2).tolist()
                        _cc_ev   = float(_cc_matched.loc[_cc_od, "eod_pct"])
                        _cc_ofig.add_trace(go.Scatter(
                            x=_cc_ox, y=_cc_oy, mode="lines",
                            line=dict(
                                color="#11F185" if _cc_ev >= 0 else "#FF3D54",
                                width=0.8,
                            ),
                            opacity=0.4, showlegend=False,
                            hovertemplate=(
                                f'{_cc_od.strftime("%b %-d, %Y")}: %{{y:+.2f}}%<extra></extra>'
                            ),
                        ))

                    _cc_ofig.add_hline(y=0, line_dash="dot", line_color="#C8C8C8", line_width=1)
                    _cc_ofig.update_layout(
                        height=400,
                        margin=dict(l=60, r=20, t=16, b=30),
                        plot_bgcolor="white", paper_bgcolor="white",
                        hovermode="closest",
                        xaxis=dict(
                            showgrid=True, gridcolor="#F0F0F0", tickformat="%H:%M",
                            range=[
                                datetime.datetime.combine(_cc_ref, datetime.time(9, 30)),
                                datetime.datetime.combine(_cc_ref, datetime.time(16, 0)),
                            ],
                            rangeslider=dict(visible=False),
                        ),
                        yaxis=dict(
                            showgrid=True, gridcolor="#F0F0F0", ticksuffix="%",
                            title=dict(text="% from open", font=dict(size=10, color="#888")),
                        ),
                    )
                    st.caption(
                        f"Showing {len(_cc_ov_dates)} most recent matching days  ·  "
                        "green = positive EOD, red = negative EOD"
                    )
                    st.plotly_chart(
                        _cc_ofig, use_container_width=True,
                        key="cc_overlay", config={"displayModeBar": False},
                    )


# =========================
# 5. EVENT IMPACT TABLE
# =========================
def _compute_event_impact(daily_df: pd.DataFrame, events: list) -> pd.DataFrame:
    """For each event type, compute count + average open-to-close and open-to-low
    on the prior trading day, event day, and next trading day."""
    if daily_df.empty or not events:
        return pd.DataFrame()

    # Open-to-close % for every trading day
    oc = (daily_df['Close'] - daily_df['Open']) / daily_df['Open'] * 100
    # Open-to-low % for every trading day (always ≤ 0)
    ol = (daily_df['Low'] - daily_df['Open']) / daily_df['Open'] * 100
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

        prior_oc  = oc.iloc[evt_loc - 1]     if evt_loc - 1 >= 0             else np.nan
        evt_oc    = oc.iloc[evt_loc]          if evt_loc < len(oc)            else np.nan
        evt_ol    = ol.iloc[evt_loc]          if evt_loc < len(ol)            else np.nan
        next_oc   = oc.iloc[evt_loc + 1]     if evt_loc + 1 < len(oc)        else np.nan

        rows.append({
            "type":     _norm(label),
            "prior_oc": prior_oc,
            "evt_oc":   evt_oc,
            "evt_ol":   evt_ol,
            "next_oc":  next_oc,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    grouped = df.groupby("type").agg(
        Count=("evt_oc", "count"),
        Prior_OC=("prior_oc", "mean"),
        Evt_OC=("evt_oc", "mean"),
        Evt_OL=("evt_ol", "mean"),
        Next_OC=("next_oc", "mean"),
    ).reset_index().rename(columns={
        "type":     "Event",
        "Prior_OC": "Prior day O→C",
        "Evt_OC":   "Event O→C",
        "Evt_OL":   "Event O→L",
        "Next_OC":  "Next day O→C",
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
        _impact_start = _today.replace(year=_today.year - _EVENT_IMPACT_YEARS)
        all_events = get_financial_events(_impact_start, _today)
        impact_df = _compute_event_impact(_event_daily, all_events)
        if impact_df.empty:
            st.info("No events found in range.")
        else:
            def _color_returns(val):
                if pd.isna(val):
                    return ''
                color = "#11F185" if val >= 0 else "#FF3D54"
                return f'color: {color}; font-weight: 600;'

            ret_cols = ["Prior day O→C", "Event O→C", "Event O→L", "Next day O→C"]
            styled = impact_df.style.format({c: "{:+.2f}%" for c in ret_cols}).map(
                _color_returns, subset=ret_cols
            )
            st.dataframe(styled, hide_index=True, use_container_width=True)
            st.markdown(
                f'<p style="font-size:11px;color:#888;margin-top:6px;">'
                f'Averaged open-to-close'
                f'<span style="margin:0 20px;"></span>'
                f'Last {_EVENT_IMPACT_YEARS} years'
                f'<span style="margin:0 20px;"></span>'
                f'{len(_event_daily):,} trading days'
                f'<span style="margin:0 20px;"></span>'
                f'{len(all_events)} event occurrences'
                f'</p>',
                unsafe_allow_html=True,
            )

# =========================
# 6. KEY DATES
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
    _kd_events = get_financial_events(datetime.date(2019, 1, 1), datetime.date.today())

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
# 7. NOTABLE EVENTS
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
# 8. ±1.5% INTRADAY MOVES
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
