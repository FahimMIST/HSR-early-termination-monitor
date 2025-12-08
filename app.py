import os
import json
from datetime import datetime
import requests
import pandas as pd
import streamlit as st

# ---------- CONFIG ----------
st.set_page_config(
    page_title="HSR Early Termination Monitor",
    layout="wide",
)

BASE_URL = "https://api.ftc.gov/v0/hsr-early-termination-notices"

STATE_FILE = "hsr_last_visit.json"


def load_last_visit():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            return data.get("last_created")
        except Exception:
            return None
    return None


def save_last_visit(last_created: str | None):
    if not last_created:
        return
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"last_created": last_created}, f)
    except Exception:
        # Failing to persist state should not break the app
        pass

# Load API key from Streamlit secrets (preferred) or env
API_KEY = st.secrets.get("FTC_API_KEY", os.getenv("FTC_API_KEY"))

if not API_KEY:
    st.error("FTC API key not found. Set `FTC_API_KEY` in .streamlit/secrets.toml or environment.")
    st.stop()

last_visit_created = load_last_visit()

# ---------- HELPER: FETCH DATA ----------
@st.cache_data(ttl=60)  # cache for 60 seconds to avoid hitting rate limits too hard
def fetch_hsr_notices(title_keyword: str | None, date_filter: str | None, limit: int = 50):
    params = {
        "api_key": API_KEY,
        "sort[created][path]": "created",
        "sort[created][direction]": "DESC",
        "page[limit]": str(limit),
    }

    # Optional: keyword in title
    if title_keyword:
        params["filter[title][operator]"] = "CONTAINS"
        params["filter[title][value]"] = title_keyword

    # Optional: exact date filter (YYYY-MM-DD)
    if date_filter:
        params["filter[date][condition][value]"] = date_filter
        params["filter[date][condition][path]"] = "date"
        # Use '=' as the operator for equality; '==' is not allowed by the API
        params["filter[date][condition][operator]"] = "="

    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", [])

    rows = []
    for it in items:
        attrs = it.get("attributes", {})
        txn_number = attrs.get("transaction-number")

        # Construct public legal-library URL using transaction number if available
        public_link = (
            f"https://www.ftc.gov/legal-library/browse/early-termination-notices/{txn_number}"
            if txn_number
            else None
        )

        rows.append(
            {
                "id": it.get("id"),
                "transaction_number": txn_number,
                "date": attrs.get("date"),
                "title": attrs.get("title"),
                "acquirer": attrs.get("acquiring-party"),
                "target": attrs.get("acquired-party"),
                "created": attrs.get("created"),
                "updated": attrs.get("updated"),
                "link": public_link,
            }
        )

    df = pd.DataFrame(rows)
    return df


# ---------- UI ----------
st.title("HSR Early Termination Monitor")
# Left-align table headers
st.markdown(
    """
    <style>
    th { text-align: left !important; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.caption("Data source: FTC HSR Early Termination Notices API")

with st.sidebar:
    st.subheader("Filters")
    keyword = st.text_input(
        "Keyword in title",
        value="",
        placeholder="Company name, deal keyword, etc.",
    ).strip() or None

    date_input = st.date_input(
        "Transaction date (optional)",
        value=None,
        help="If set, shows only notices with this transaction date.",
    )
    date_filter = date_input.strftime("%Y-%m-%d") if date_input else None

    limit = st.number_input(
        "Max records",
        min_value=10,
        max_value=200,
        value=50,
        step=10,
        help="Number of latest notices to fetch.",
    )

    st.markdown("---")
    st.caption("Tip: Clear filters to see the full latest feed.")

# Auto-refresh every 5 minutes (300,000 ms) or on manual rerun
st_autorefresh = st.autorefresh = getattr(st, "autorefresh", None)
if st_autorefresh:
    st_autorefresh(interval=5 * 60 * 1000, key="hsr_autorefresh")

# ---------- FETCH + DISPLAY ----------
try:
    df = fetch_hsr_notices(keyword, date_filter, limit)

    if df.empty:
        st.info("No early termination notices found for the current filters.")
    else:
        # Determine latest created timestamp and mark new rows since last visit
        latest_created = df["created"].max() if "created" in df.columns and not df["created"].isna().all() else None

        if last_visit_created and "created" in df.columns:
            df["is_new"] = df["created"] > last_visit_created
        else:
            df["is_new"] = False

        new_count = int(df["is_new"].sum())

        # Prepare a friendly status column
        df["status"] = df["is_new"].map(lambda x: "🟢 New" if x else "")

        latest_date = df["date"].max() if "date" in df.columns else None
        total_notices = len(df)

        # Summary metrics
        m1, m2, m3 = st.columns(3)
        m1.metric("Total notices", total_notices)
        m2.metric("New since last visit", new_count)
        if latest_date:
            m3.metric("Latest transaction date", str(latest_date))
        else:
            m3.metric("Latest transaction date", "N/A")

        st.markdown("### Results")

        # Sort controls (server-side sorting)
        sort_options = {
            "Date": "date",
            "Acquirer": "acquirer",
            "Target": "target",
            "Title": "title",
            "Link": "link",
            "Transaction number": "transaction_number",
            "Status": "status",
        }
        sort_col_label = st.selectbox("Sort by", list(sort_options.keys()), index=0)
        sort_dir = st.radio("Order", ["Descending", "Ascending"], index=0, horizontal=True)
        sort_col = sort_options[sort_col_label]

        df = df.sort_values(
            by=sort_col,
            ascending=(sort_dir == "Ascending"),
            na_position="last",
        ).reset_index(drop=True)

        # Reorder columns for display (status moved to end, hidden columns removed)
        cols_order = [
            "date",
            "target",
            "acquirer",
            "title",
            "link",
            "transaction_number",
            "status",
        ]

        # df_logic keeps original column names for logic and detail view
        df_logic = df[[c for c in cols_order if c in df.columns]].copy()

        # df_table is for display (with pretty headers)
        df_table = df_logic.copy()
        df_table.columns = [col.capitalize().replace("_", " ") for col in df_table.columns]

        # Export button (uses display table as-is)
        csv = df_table.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download as CSV",
            data=csv,
            file_name="hsr_early_terminations.csv",
            mime="text/csv",
        )

        # Make link column clickable using Markdown/HTML (display only)
        if "Link" in df_table.columns:
            df_table["Link"] = df_table["Link"].apply(
                lambda x: f'<a href="{x}" target="_blank">Open Filing</a>' if pd.notna(x) and x else ""
            )

        # Styled HTML table with sticky header and alternating row colors
        table_html = df_table.to_html(escape=False, index=False, classes="hsr-table")

        st.markdown(
            """
            <style>
            .hsr-table-container {
                max-height: 600px;
                overflow-y: auto;
                border: 1px solid #eee;
                border-radius: 4px;
            }
            .hsr-table-container table {
                border-collapse: collapse;
                width: 100%;
            }
            .hsr-table-container thead th {
                position: sticky;
                top: 0;
                background-color: #ffffff;
                z-index: 1;
                text-align: left;
            }
            .hsr-table-container tbody tr:nth-child(odd) {
                background-color: #fafafa;
            }
            .hsr-table-container tbody tr:nth-child(even) {
                background-color: #ffffff;
            }
            .hsr-table-container tbody tr:hover {
                background-color: #f0f0f5;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        st.markdown(
            f'<div class="hsr-table-container">{table_html}</div>',
            unsafe_allow_html=True,
        )

        # Simple detail viewer
        st.markdown("### Notice details")
        if "transaction_number" in df_logic.columns and not df_logic.empty:
            selected_txn = st.selectbox(
                "Select a transaction to inspect",
                options=df_logic["transaction_number"],
                format_func=lambda x: f"{x}" if pd.notna(x) else "N/A",
            )
            detail_row = df_logic[df_logic["transaction_number"] == selected_txn].iloc[0]
            st.write(
                {
                    "Title": detail_row.get("title"),
                    "Date": detail_row.get("date"),
                    "Acquirer": detail_row.get("acquirer"),
                    "Target": detail_row.get("target"),
                    "Transaction number": detail_row.get("transaction_number"),
                }
            )

        # Optional: show raw data toggle for debugging
        with st.expander("Debug / raw data"):
            st.write(df.head(20))

        # Persist latest created timestamp for next visit
        if latest_created:
            save_last_visit(latest_created)

except requests.HTTPError as e:
    st.error(f"HTTP error from FTC API: {e}")
except Exception as e:
    st.error(f"Unexpected error: {e}")