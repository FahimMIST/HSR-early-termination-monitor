import time
from app import check_and_send_hsr_alerts

if __name__ == "__main__":
    # Simple loop: check every 5 minutes
    # For production, prefer cron or a proper scheduler
    CHECK_INTERVAL_SECONDS = 300  # 5 minutes

    while True:
        try:
            check_and_send_hsr_alerts(limit=50)
        except Exception as e:
            # In a real setup, log this somewhere instead of print
            print(f"[monitor] Error while checking HSR notices: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)