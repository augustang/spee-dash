"""Bespoke floating navigation bar rendered at the top of every page."""
import streamlit as st


def render_nav(current: str) -> None:
    """Inject a fixed floating nav bar.

    Parameters
    ----------
    current : str
        The active page slug — "trading" or "study".
    """
    trading_active = current == "trading"
    study_active   = current == "study"

    def _link(label: str, path: str, active: bool) -> str:
        active_style = (
            "color: #1A1A1A; font-weight: 600;"
        ) if active else (
            "color: #555; font-weight: 400;"
        )
        return (
            f'<a href="/{path}" target="_self" '
            f'style="text-decoration: none; font-size: 13px; {active_style} '
            f'cursor: pointer;">'
            f'{label}</a>'
        )

    trading_link = _link("Trading", "",      trading_active)
    study_link   = _link("Study",   "study", study_active)

    nav_html = f"""
<style>
  .spx-nav {{
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 9999;
    background: rgba(244, 244, 244, 0.92);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: none;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 10px 2rem;
    box-sizing: border-box;
    gap: 2.5rem;
  }}
</style>
<div class="spx-nav">
  {trading_link}
  {study_link}
</div>
"""
    st.markdown(nav_html, unsafe_allow_html=True)
