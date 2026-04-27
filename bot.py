import os
import csv
import base64
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
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

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


def _upload_to_imgbb(image_bytes):
    """Upload raw image bytes to imgbb and return the public URL."""
    resp = requests.post(
        "https://api.imgbb.com/1/upload",
        params={"key": IMGBB_API_KEY},
        data={"image": base64.b64encode(image_bytes).decode("utf-8")},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["url"]


def fetch_image_url(event_name, image_prompt):
    """Return a high-quality image URL.
    Priority: HF FLUX.1 (1024px AI) → Pexels (1880px photo) → Pollinations (fallback).
    """
    # 1. Hugging Face FLUX.1-schnell — free, 1024x1024 AI-generated
    if HF_TOKEN and IMGBB_API_KEY:
        try:
            print("Generating image with HF FLUX.1-schnell...")
            hf_resp = requests.post(
                "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell",
                headers={"Authorization": f"Bearer {HF_TOKEN}"},
                json={"inputs": image_prompt, "parameters": {"width": 1024, "height": 1024, "num_inference_steps": 4}},
                timeout=120,
            )
            hf_resp.raise_for_status()
            url = _upload_to_imgbb(hf_resp.content)
            print(f"HF FLUX image hosted at: {url}")
            return url
        except Exception as e:
            print(f"HF FLUX error: {e} — trying Pexels")

    # 2. Pexels — free, high-res professional photos
    if PEXELS_API_KEY:
        try:
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": event_name, "per_page": 1, "orientation": "square"},
                timeout=15,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
            if photos:
                url = photos[0]["src"]["large2x"]
                print(f"Pexels image: {url}")
                return url
            print("Pexels: no results, falling back to Pollinations")
        except Exception as e:
            print(f"Pexels error: {e} — falling back to Pollinations")

    # 3. Pollinations — no API key needed, lower quality
    encoded = urllib.parse.quote(image_prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model=flux"


def build_facebook_post(title, caption, hashtags):
    return f"{title}\n\n{caption}\n\n{hashtags}"


def build_instagram_caption(title, caption, hashtags):
    return f"{title}\n\n{caption}\n\n{hashtags}"


def post_to_facebook(message, image_url):
    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos"
    response = requests.post(
        url,
        data={"url": image_url, "caption": message, "access_token": FB_PAGE_TOKEN},
        timeout=30,
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

    # Step 2: wait until Instagram finishes processing the image
    status_url = f"https://graph.facebook.com/v19.0/{creation_id}"
    for attempt in range(12):
        status_resp = requests.get(
            status_url,
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
            timeout=10,
        )
        status_resp.raise_for_status()
        status_code = status_resp.json().get("status_code")
        print(f"Instagram container status: {status_code}")
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError(f"Instagram container failed to process: {status_resp.json()}")
        time.sleep(5)
    else:
        raise TimeoutError("Instagram container was not ready after 60s")

    # Step 3: publish the container
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

        image_url = fetch_image_url(event_name, image_prompt)
        print(f"Image URL: {image_url}")

        fb_text = build_facebook_post(title, caption, hashtags)
        fb_result = post_to_facebook(fb_text, image_url)
        print(f"Facebook: posted successfully. Post ID: {fb_result.get('id')}")

        if IG_USER_ID:
            ig_caption = build_instagram_caption(title, caption, hashtags)
            ig_result = post_to_instagram(ig_caption, image_url)
            print(f"Instagram: posted successfully. Post ID: {ig_result.get('id')}")
        else:
            print("Instagram: skipped — INSTAGRAM_USER_ID not configured.")


if __name__ == "__main__":
    main()
