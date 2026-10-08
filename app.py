from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Stock Screener", layout="wide")

# One app, two screeners, switched from the tab bar at the top. st.navigation
# only runs the page that's open, so the live momentum scanner never slows
# down the value page (and vice versa).
page = st.navigation(
    [
        st.Page("views/value_page.py", title="Value", icon=":material/account_balance:", url_path="value", default=True),
        st.Page("views/momentum_page.py", title="Momentum", icon=":material/rocket_launch:", url_path="momentum"),
    ],
    position="top",
)
page.run()
