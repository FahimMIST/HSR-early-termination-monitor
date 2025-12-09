import sys
from app import check_and_send_hsr_alerts

def main():
    """
    One-shot monitor script for GitHub Actions / cron.

    It just runs a single check for new HSR notices,
    sends Brevo + Slack alerts if needed, then exits.
    """
    try:
        print("🔎 Running HSR early termination monitor (single run)...")
        # You can tweak limit if you want to scan more rows
        check_and_send_hsr_alerts(limit=50)
        print("✅ Monitor run finished.")
    except Exception as e:
        # Non-zero exit so GitHub Actions marks the job as failed if something breaks
        print(f"[monitor] Error while checking HSR notices: {e}", file=sys.stderr)
        raise

if __name__ == "__main__":
    main()