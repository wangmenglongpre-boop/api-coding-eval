"""Small native UI polish; no external fonts, images or scripts."""

import streamlit as st


def apply_style() -> None:
    st.markdown("""
    <style>
    .stApp {background: #f6f8fc;}
    [data-testid="stHeader"] {background: rgba(246,248,252,.95);}
    [data-testid="stSidebar"] {background: #edf2fa; border-right: 1px solid #dde5f0;}
    [data-testid="stMainBlockContainer"] {max-width: 1440px; padding-top: 2rem; padding-bottom: 3rem;}
    h1 {letter-spacing: -.035em; color: #172b4d;}
    h2,h3 {color: #23395d;}
    [data-testid="stMetric"] {
      background: white; border: 1px solid #e0e7f1; border-radius: 12px;
      padding: 1rem 1.15rem; box-shadow: 0 2px 8px #17365d05;
    }
    [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
    [data-testid="stForm"], [data-testid="stExpander"] {
      background: white; border-color: #e0e7f1; border-radius: 12px;
    }
    [data-testid="stDataFrame"] {border-radius: 10px; overflow: hidden;}
    [data-testid="stButton"] button, [data-testid="stDownloadButton"] button {border-radius: 8px;}
    [data-testid="stCode"] pre {max-height: 480px; overflow: auto;}
    @media(max-width: 720px) {
      [data-testid="stMainBlockContainer"] {padding: 4rem .8rem 2rem;}
      .stApp h1 {font-size: 1.9rem;}
      [data-testid="stHorizontalBlock"] {flex-wrap: wrap;}
      [data-testid="stColumn"] {min-width: min(100%, 260px); flex: 1 1 260px;}
      [data-testid="stMetric"] {padding: .75rem;}
    }
    </style>
    """, unsafe_allow_html=True)
