import os
import json
from datetime import datetime
import requests
import pandas as pd
import streamlit as st
from string import Template

# Config helper to load secrets/env
def get_config_value(key: str, default: str | None = None) -> str | None:
    """
    Load configuration / secrets in a way that works both:
      - locally / Streamlit (st.secrets + .env)
      - GitHub Actions / other CLIs (environment variables only)
    """
    # 1. Prefer environment variables (GitHub Actions, Render, etc.)
    val = os.getenv(key)
    if val:
        return val

    # 2. Fall back to Streamlit secrets if available
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        # No secrets.toml or cannot parse; ignore and use default
        pass

    return default


# Global configuration values
FTC_API_KEY = get_config_value("FTC_API_KEY")
BREVO_API_KEY = get_config_value("BREVO_API_KEY")
ALERT_EMAIL_TO = get_config_value("ALERT_EMAIL_TO")
SLACK_WEBHOOK_URL = get_config_value("SLACK_WEBHOOK_URL")
from sib_api_v3_sdk import Configuration, ApiClient
from sib_api_v3_sdk.api.transactional_emails_api import TransactionalEmailsApi
from sib_api_v3_sdk.models.send_smtp_email import SendSmtpEmail

# Slack notification function
def send_slack_alert(payload: dict):
    """
    Send a Slack notification using an incoming webhook.

    Expects a full payload dict (supports Block Kit).
    """
    if not SLACK_WEBHOOK_URL:
        return  # Slack not configured

    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        st.error(f"Slack notification failed: {e}")


BASE_URL = "https://api.ftc.gov/v0/hsr-early-termination-notices"

STATE_FILE = "hsr_last_visit.json"
SUBSCRIBERS_FILE = "hsr_subscribers.json"

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
ALERT_TEMPLATE_FILE = os.path.join(TEMPLATE_DIR, "hsr_alert_email.html")
SLACK_TEMPLATE_FILE = os.path.join(TEMPLATE_DIR, "hsr_slack_item.txt")

# Brevo email config (optional, used if configured)
# Values are already loaded via get_config_value at the top:
#   BREVO_API_KEY
#   ALERT_EMAIL_TO


def render_hsr_email_html(new_items: pd.DataFrame) -> tuple[str, str]:
    """
    Render the HSR alert email HTML using a template file if present.
    The template should use $count and $items placeholders.
    """
    count = len(new_items)

    items_html_lines: list[str] = []
    for _, row in new_items.iterrows():
        link = row.get("link") or ""
        link_html = f'<a href="{link}">Open filing</a>' if link else ""
        items_html_lines.append(
            f"<li><strong>{row.get('date')} – {row.get('title')}</strong><br>"
            f"Acquirer: {row.get('acquirer') or 'N/A'}<br>"
            f"Target: {row.get('target') or 'N/A'}<br>"
            f"{link_html}</li>"
        )

    items_html = "\n".join(items_html_lines)

    try:
        with open(ALERT_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        # Simple fallback template if file is missing
        template = """
        <h2>$count new HSR early termination notice(s)</h2>
        <p>The following notices are new since the last alert:</p>
        <ul>
        $items
        </ul>
        """

    html = Template(template).safe_substitute(count=count, items=items_html)
    subject = f"{count} new HSR early termination notice(s)"
    return subject, html


# Helper: Render Slack Block Kit payload using template file
def render_slack_payload(new_items: pd.DataFrame) -> dict:
    """
    Build a Slack Block Kit payload for new HSR notices.

    Uses a per-item text template from SLACK_TEMPLATE_FILE if present.
    The template can use:
      {date}, {title}, {acquirer}, {target}, {link}
    where {link} is already formatted as Slack markdown (e.g. "<url|Open filing>").
    """
    count_new = len(new_items)

    # Load per-item template or use a default
    try:
        with open(SLACK_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            item_template = f.read().strip()
    except FileNotFoundError:
        item_template = (
            "*{date}* — *{title}*\n"
            "*Acquirer:* {acquirer}\n"
            "*Target:* {target}\n"
            "{link}"
        )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔔 {count_new} new HSR Early Termination notice(s)",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Here are the latest filings detected:",
            },
        },
        {"type": "divider"},
    ]

    # Slack Block Kit has limits (max 50 blocks per message). We reserve 3 blocks
    # for header/intro/divider, so we can include up to 47 item sections. Keep a
    # small buffer and cap at 45 items.
    max_items = 45
    shown = 0

    for _, row in new_items.iterrows():
        if shown >= max_items:
            break
        date_val = row.get("date") or ""
        title_val = row.get("title") or ""
        acquirer_val = row.get("acquirer") or "N/A"
        target_val = row.get("target") or "N/A"
        link_val = row.get("link") or ""

        link_markdown = f"<{link_val}|Open filing>" if link_val else ""

        text_body = item_template.format(
            date=date_val,
            title=title_val,
            acquirer=acquirer_val,
            target=target_val,
            link=link_markdown,
        )

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": text_body,
                },
            }
        )
        shown += 1

    if count_new > shown:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"...and {count_new - shown} more new notice(s).",
                    }
                ],
            }
        )

    payload = {
        "text": f"{count_new} new HSR Early Termination notice(s)",
        "blocks": blocks,
    }
    return payload


def send_hsr_email(subject: str, html_content: str):
    """
    Send an HSR alert email via Brevo.

    Recipients are taken from the subscribers JSON file if available.
    If no subscribers are stored, falls back to ALERT_EMAIL_TO.
    If neither is configured, this is a no-op.
    """
    if not BREVO_API_KEY:
        # No API key configured, nothing to do
        return

    # Build recipient list from subscribers file
    try:
        subscribers = load_subscribers()
    except Exception:
        subscribers = []

    recipients: list[dict] = []
    for sub in subscribers:
        email_addr = (sub.get("email") or "").strip()
        if email_addr:
            recipients.append({"email": email_addr})

    # Fallback: single configured ALERT_EMAIL_TO if no subscribers
    if not recipients and ALERT_EMAIL_TO:
        recipients.append({"email": ALERT_EMAIL_TO})

    # If we still have no recipients, silently skip
    if not recipients:
        return

    configuration = Configuration()
    configuration.api_key["api-key"] = BREVO_API_KEY

    api_client = ApiClient(configuration)
    api_instance = TransactionalEmailsApi(api_client)

    email = SendSmtpEmail(
        to=recipients,
        sender={"email": "alerts@hsr-monitor.local", "name": "HSR Early Termination Monitor"},
        subject=subject,
        html_content=html_content,
    )

    try:
        api_instance.send_transac_email(email)
    except Exception as e:
        # Surface errors in the UI but do not break the app
        st.error(f"Failed to send Brevo email alert: {e}")


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


def load_subscribers() -> list[dict]:
    """Load stored subscriber emails from JSON file."""
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Expect a list of objects; fall back to empty list if malformed
            if isinstance(data, list):
                return data
        except Exception:
            return []
    return []


def save_subscribers(subscribers: list[dict]) -> None:
    """Persist subscriber list to JSON file."""
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(subscribers, f, indent=2)
    except Exception:
        # Failing to persist subscribers should not break the app
        pass


# Ensure FTC API key is configured
if not FTC_API_KEY:
    raise RuntimeError("FTC API key not found. Set `FTC_API_KEY` as an environment variable or in .streamlit/secrets.toml.")


@st.cache_data(ttl=60)  # cache for 60 seconds to avoid hitting rate limits too hard
def fetch_hsr_notices(title_keyword: str | None, date_filter: str | None, limit: int = 50):
    params = {
        "api_key": FTC_API_KEY,
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


def check_and_send_hsr_alerts(limit: int = 50):
    """
    Backend helper: check FTC for new HSR notices and send email + Slack alerts
    for any items with `created` > last_alert_created stored in STATE_FILE.

    This is intended to be called by a background job (cron, worker script, etc.)
    and should NOT be invoked from the Streamlit UI code.
    """
    last_alert_created = load_last_visit()

    # Fetch latest notices without any filters
    df = fetch_hsr_notices(title_keyword=None, date_filter=None, limit=limit)

    if df.empty or "created" not in df.columns:
        return

    # Determine the latest created timestamp in this batch
    latest_created = df["created"].max() if not df["created"].isna().all() else None

    if not latest_created:
        return

    # Select items that are newer than the last alert
    if last_alert_created:
        new_items = df[df["created"] > last_alert_created]
    else:
        # First run: treat all fetched items as new
        new_items = df.copy()

    if new_items.empty:
        # Nothing new to alert on
        return

    # Send email + Slack alerts for new items
    subject, html_body = render_hsr_email_html(new_items)
    send_hsr_email(subject, html_body)

    slack_payload = render_slack_payload(new_items)
    send_slack_alert(slack_payload)

    # Persist the newest created timestamp so we don't alert again for the same rows
    save_last_visit(latest_created)


def main():
    # ---------- CONFIG ----------
    st.set_page_config(
        page_title="HSR Early Termination Monitor",
        layout="wide",
    )

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

        st.markdown("### Subscribe for email alerts")
        with st.form("subscribe_form"):
            sub_email = st.text_input("Email address", placeholder="you@example.com")
            sub_name = st.text_input("Name (optional)", placeholder="Your name (optional)")
            submit_sub = st.form_submit_button("Subscribe")

        if submit_sub:
            if not sub_email or "@" not in sub_email:
                st.warning("Please enter a valid email address.")
            else:
                subscribers = load_subscribers()
                normalized = sub_email.strip().lower()
                already = any((s.get("email") or "").strip().lower() == normalized for s in subscribers)
                if already:
                    st.info("This email is already subscribed.")
                else:
                    subscribers.append(
                        {
                            "email": sub_email.strip(),
                            "name": sub_name.strip() or None,
                            "created": datetime.utcnow().isoformat() + "Z",
                        }
                    )
                    save_subscribers(subscribers)
                    st.success("You have been subscribed to HSR alerts.")

        st.markdown("---")
        st.caption("Tip: Clear filters to see the full latest feed.")

    # Auto-refresh every 5 minutes (300,000 ms) or on manual rerun
    st_autorefresh = getattr(st, "autorefresh", None)
    if st_autorefresh:
        st_autorefresh(interval=5 * 60 * 1000, key="hsr_autorefresh")

    # ---------- FETCH + DISPLAY ----------
    try:
        df = fetch_hsr_notices(keyword, date_filter, limit)

        if df.empty:
            st.info("No early termination notices found for the current filters.")
        else:
            latest_date = df["date"].max() if "date" in df.columns else None
            total_notices = len(df)

            # Summary metrics
            m1, m2, m3 = st.columns(3)
            m1.metric("Total notices in table", total_notices)
            m2.metric("Alert mode", "Real-time monitor")
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
            }
            sort_col_label = st.selectbox("Sort by", list(sort_options.keys()), index=0)
            sort_dir = st.radio("Order", ["Descending", "Ascending"], index=0, horizontal=True)
            sort_col = sort_options[sort_col_label]

            df = df.sort_values(
                by=sort_col,
                ascending=(sort_dir == "Ascending"),
                na_position="last",
            ).reset_index(drop=True)

            # Reorder columns for display (no status / per-visit new flags)
            cols_order = [
                "date",
                "target",
                "acquirer",
                "title",
                "link",
                "transaction_number",
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

            # Styled HTML table with sticky header and alternating row colors, theme-aware
            table_html = df_table.to_html(escape=False, index=False, classes="hsr-table")

            # Detect Streamlit base theme (light or dark) and choose colors accordingly
            theme_base = st.get_option("theme.base") or "light"
            if theme_base.lower() == "dark":
                header_bg = "#111827"
                header_text = "#e5e7eb"
                border_color = "#111827"
                row_odd = "#020617"
                row_even = "#0b1120"
                row_hover = "#1f2937"
                cell_text = "#e5e7eb"
                link_color = "#60a5fa"
            else:
                # Light theme palette
                header_bg = "#f3f4f6"
                header_text = "#111827"
                border_color = "#e5e7eb"
                row_odd = "#ffffff"
                row_even = "#f9fafb"
                row_hover = "#e5e7eb"
                cell_text = "#111827"
                link_color = "#2563eb"

            table_css = f"""
            <style>
            .hsr-table-container {{
                max-height: 600px;
                overflow-y: auto;
                border: 1px solid {border_color};
                border-radius: 6px;
            }}
            .hsr-table-container table {{
                border-collapse: collapse;
                width: 100%;
                font-size: 0.9rem;
            }}
            .hsr-table-container thead th {{
                position: sticky;
                top: 0;
                background-color: {header_bg};
                color: {header_text};
                z-index: 2;
                text-align: left;
                padding: 0.6rem 0.75rem;
                border-bottom: 1px solid {border_color};
            }}
            .hsr-table-container tbody td {{
                padding: 0.55rem 0.75rem;
                border-bottom: 1px solid {border_color};
                color: {cell_text};
            }}
            .hsr-table-container tbody tr:nth-child(odd) {{
                background-color: {row_odd};
            }}
            .hsr-table-container tbody tr:nth-child(even) {{
                background-color: {row_even};
            }}
            .hsr-table-container tbody tr:hover {{
                background-color: {row_hover};
            }}
            .hsr-table-container a {{
                color: {link_color};
                text-decoration: none;
                font-weight: 500;
            }}
            .hsr-table-container a:hover {{
                text-decoration: underline;
            }}
            </style>
            """

            st.markdown(table_css, unsafe_allow_html=True)

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

    except requests.HTTPError as e:
        st.error(f"HTTP error from FTC API: {e}")
    except Exception as e:
        st.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()