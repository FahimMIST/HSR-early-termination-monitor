import os
import sys
from app import check_and_send_hsr_alerts

def main():
    """
    One-shot monitor script for GitHub Actions / cron.

    It just runs a single check for new HSR notices,
    sends Brevo + Slack alerts if needed, then exits.
    """
    # Ensure relative paths (e.g., hsr_last_visit.json, templates/) resolve correctly
    # regardless of where the script is invoked from.
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)

    # Optional: allow overriding the scan limit via env var
    limit = int(os.getenv("HSR_MONITOR_LIMIT", "50"))

    try:
        print("🔎 Running HSR early termination monitor (single run)...")
        # You can tweak limit if you want to scan more rows
        check_and_send_hsr_alerts(limit=limit)
        print("✅ Monitor run finished.")
    except Exception as e:
        # Non-zero exit so GitHub Actions marks the job as failed if something breaks
        print(f"[monitor] Error while checking HSR notices: {e}", file=sys.stderr)
        raise

if __name__ == "__main__":
    main()