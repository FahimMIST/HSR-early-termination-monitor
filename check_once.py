from app import check_and_send_hsr_alerts

if __name__ == "__main__":
    # One-shot check for new HSR notices and alerts
    check_and_send_hsr_alerts(limit=50)