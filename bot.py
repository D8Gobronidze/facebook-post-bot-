import os
import csv
import requests
from datetime import datetime
from groq import Groq
import io

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
FB_PAGE_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID = "706511265884553"
SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1hzH4sVjs6SUPqD_4S0hM0ZvGz6anZQpYSVeInHvl66Y"
    "/export?format=csv&gid=472347742"
)


def fetch_events():
    response = requests.get(SHEET_CSV_URL, timeout=15)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader)


def parse_date(date_str):
    date_str = date_str.strip()
    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
        "%B %d, %Y", "%b %d, %Y", "%d-%m-%Y",
        "%m-%d-%Y", "%d.%m.%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def get_todays_events(events):
    today = datetime.today().strftime("%Y-%m-%d")
    matched = []
    for event in events:
        date_str = event.get("Date", "")
        parsed = parse_date(date_str)
        if parsed and parsed.strftime("%Y-%m-%d") == today:
            matched.append(event)
    return matched


def generate_post(event_name, status):
    client = Groq(api_key=GROQ_API_KEY)
    prompt = (
        f"Create an engaging Facebook post for this event:\n"
        f"Event: {event_name}\n"
        f"Status: {status}\n\n"
        f"Make it warm, friendly, and suitable for a Facebook page audience. "
        f"Include relevant emojis. Keep it under 200 words. "
        f"Write only the post text, nothing else."
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=350,
    )
    return response.choices[0].message.content.strip()


def post_to_facebook(message):
    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
    response = requests.post(
        url,
        data={"message": message, "access_token": FB_PAGE_TOKEN},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def main():
    today_str = datetime.today().strftime("%Y-%m-%d")
    print(f"Running bot for {today_str}")

    events = fetch_events()
    today_events = get_todays_events(events)

    if not today_events:
        print("No events scheduled for today. Nothing to post.")
        return

    for event in today_events:
        event_name = event.get("Event Name", "").strip()
        status = event.get("Status", "").strip()

        if not event_name:
            continue

        print(f"Generating post for: {event_name} [{status}]")
        post_text = generate_post(event_name, status)
        print(f"Generated post:\n{post_text}\n")

        result = post_to_facebook(post_text)
        print(f"Successfully posted! Post ID: {result.get('id')}")


if __name__ == "__main__":
    main()
