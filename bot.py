import os
import csv
import requests
from datetime import datetime
from groq import Groq
import io
import json
import time
import urllib.parse

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
FB_PAGE_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID = "706511265884553"

IG_USER_ID = os.environ.get("INSTAGRAM_USER_ID", "")
IG_ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", FB_PAGE_TOKEN)

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


def generate_content(event_name, status):
    """Ask Groq to generate the full post: title, caption, hashtags, and image prompt."""
    client = Groq(api_key=GROQ_API_KEY)
    prompt = (
        f"You are a social media manager. Generate content for this event:\n"
        f"Event: {event_name}\n"
        f"Status: {status}\n\n"
        f"Return ONLY a valid JSON object with these exact keys:\n"
        f"  title: a short punchy headline (max 10 words)\n"
        f"  caption: warm friendly post body with emojis (max 150 words)\n"
        f"  hashtags: a string of 10-15 relevant hashtags separated by spaces\n"
        f"  image_prompt: a detailed visual description for an AI image generator "
        f"(describe the scene, style, colors, mood — no text or logos in the image)\n\n"
        f"Example format:\n"
        f'{{"title": "...", "caption": "...", "hashtags": "#tag1 #tag2", "image_prompt": "..."}}\n\n'
        f"Return only the JSON, no extra text."
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
    )
    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


def build_image_url(image_prompt):
    """Generate a free image via Pollinations.ai — no API key needed."""
    encoded = urllib.parse.quote(image_prompt)
    # 1080x1080 is ideal for Instagram square posts
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1080&nologo=true"


def build_facebook_post(title, caption, hashtags):
    return f"{title}\n\n{caption}\n\n{hashtags}"


def build_instagram_caption(title, caption, hashtags):
    return f"{title}\n\n{caption}\n\n{hashtags}"


def post_to_facebook(message):
    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
    response = requests.post(
        url,
        data={"message": message, "access_token": FB_PAGE_TOKEN},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def ensure_image_ready(image_url, retries=3, delay=10):
    """Hit the Pollinations URL to trigger generation and wait until it responds with an image."""
    for attempt in range(retries):
        try:
            resp = requests.get(image_url, timeout=60)
            content_type = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and "image" in content_type:
                print(f"Image ready (attempt {attempt + 1}), content-type: {content_type}")
                return True
        except requests.RequestException as e:
            print(f"Image fetch attempt {attempt + 1} failed: {e}")
        if attempt < retries - 1:
            print(f"Waiting {delay}s before retry...")
            time.sleep(delay)
    print("Warning: could not confirm image is ready — proceeding anyway")
    return False


def post_to_instagram(caption, image_url):
    """Post a photo to Instagram using the Meta Graph API."""
    ensure_image_ready(image_url)

    # Step 1: create media container
    container_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media"
    container_resp = requests.post(
        container_url,
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=30,
    )
    if not container_resp.ok:
        print(f"Instagram container error {container_resp.status_code}: {container_resp.text}")
    container_resp.raise_for_status()
    creation_id = container_resp.json().get("id")
    print(f"Instagram: media container created (id: {creation_id})")

    # Step 2: publish the container
    publish_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media_publish"
    publish_resp = requests.post(
        publish_url,
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
        timeout=30,
    )
    if not publish_resp.ok:
        print(f"Instagram publish error {publish_resp.status_code}: {publish_resp.text}")
    publish_resp.raise_for_status()
    return publish_resp.json()


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

        print(f"\nGenerating content for: {event_name} [{status}]")
        content = generate_content(event_name, status)

        title = content.get("title", event_name)
        caption = content.get("caption", "")
        hashtags = content.get("hashtags", "")
        image_prompt = content.get("image_prompt", f"vibrant event poster for {event_name}")

        print(f"Title: {title}")
        print(f"Caption: {caption}")
        print(f"Hashtags: {hashtags}")
        print(f"Image prompt: {image_prompt}")

        image_url = build_image_url(image_prompt)
        print(f"Image URL: {image_url}")

        fb_text = build_facebook_post(title, caption, hashtags)
        fb_result = post_to_facebook(fb_text)
        print(f"Facebook: posted successfully. Post ID: {fb_result.get('id')}")

        if IG_USER_ID:
            ig_caption = build_instagram_caption(title, caption, hashtags)
            ig_result = post_to_instagram(ig_caption, image_url)
            print(f"Instagram: posted successfully. Post ID: {ig_result.get('id')}")
        else:
            print("Instagram: skipped — INSTAGRAM_USER_ID not configured.")


if __name__ == "__main__":
    main()
