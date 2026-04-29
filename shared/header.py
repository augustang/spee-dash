"""Shared CSS loader and market-status header rendered on every page."""
import datetime

import pytz
import streamlit as st

import schwab_client


def load_css(file_name):
    try:
        with open(file_name) as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning("Could not find style.css. Make sure it is in the same folder as app.py")


@st.cache_data(ttl=300, show_spinner=False)
def _get_market_hours():
    return schwab_client.fetch_market_hours()


def render_header(current_page: str = None):
    """Render the date / time / market-status strip at the top of the app.

    Parameters
    ----------
    current_page : str, optional
        When provided ("trading" or "study"), nav links are rendered
        right-aligned on the same row as the date/time text.
    """
    eastern = pytz.timezone('America/New_York')
    now = datetime.datetime.now(eastern)
    date_str = now.strftime("%A %B %-d, %Y")
    time_str = now.strftime("%H:%M")

    market_info = _get_market_hours()

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

    def _nav_link(label, path, active):
        weight = "600" if active else "400"
        color = "#1A1A1A" if active else "#888"
        return (
            f'<a href="/{path}" target="_self" style="text-decoration: none; '
            f'font-size: 14px; font-weight: {weight}; color: {color}; cursor: pointer;">'
            f'{label}</a>'
        )

    nav_html = ""
    if current_page is not None:
        trade_link = _nav_link("Trade", "",      current_page == "trading")
        study_link = _nav_link("Study", "study", current_page == "study")
        nav_html = (
            f'<div style="display: flex; gap: 1.5rem; align-items: center; '
            f'background: white; border-radius: 12px; padding: 5px 16px;">'
            f'{trade_link}{study_link}'
            f'</div>'
        )

    header_html = f"""
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
            <div style="font-size: 16px; font-weight: 400;">
                <span style="color: black;"><b>{date_str.split(' ')[0]}</b> {' '.join(date_str.split(' ')[1:])}</span>
                <span style="color: #B71AFF; margin-left: 15px; font-weight: 400;">{time_str}</span>
                <span style="color: #D57CFF; font-size: 16px; font-weight: 400;">{status_str}</span>
            </div>
            {nav_html}
        </div>
    """
    st.markdown(header_html, unsafe_allow_html=True)
