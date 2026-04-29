import json
import os

import streamlit as st

from shared.header import load_css

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

st.set_page_config(page_title="SPX Dashboard", layout="wide")
load_css("style.css")

# Inject sidebar-hide rule as early as possible to prevent the sidebar
# from flashing before the full style.css is applied by React.
st.markdown(
    '<style>'
    '[data-testid="stSidebar"],'
    '[data-testid="stSidebarCollapsedControl"],'
    '[data-testid="collapsedControl"],'
    'header[data-testid="stHeader"]'
    '{display:none!important}'
    '</style>',
    unsafe_allow_html=True,
)

pg = st.navigation([
    st.Page("views/trading.py", title="Trading", icon="📈", default=True, url_path="trading"),
    st.Page("views/study.py",   title="Study",   icon="🔬",              url_path="study"),
])
pg.run()
