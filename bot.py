import os
import csv
import base64
import random
import requests
from datetime import datetime
from groq import Groq
import io
import json
import time
import urllib.parse

from PIL import Image, ImageStat
from huggingface_hub import InferenceClient

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
FB_PAGE_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
FB_PAGE_ID = "706511265884553"

IG_USER_ID = os.environ.get("INSTAGRAM_USER_ID", "")
IG_ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", FB_PAGE_TOKEN)
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1hzH4sVjs6SUPqD_4S0hM0ZvGz6anZQpYSVeInHvl66Y"
    "/export?format=csv&gid=472347742"
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_DARK_PATH = os.path.join(_BASE_DIR, "logo_dark.png")
LOGO_WHITE_PATH = os.path.join(_BASE_DIR, "logo_white.png")

EVENTS_CACHE_NAME = "events_cache.csv"
EVENTS_CACHE_PATH = os.path.join(_BASE_DIR, EVENTS_CACHE_NAME)

for _p in (LOGO_DARK_PATH, LOGO_WHITE_PATH):
    print(f"Logo file {'FOUND' if os.path.exists(_p) else 'MISSING'}: {_p}")

TEST_IMAGE_PROMPT = ""  # set to a prompt string to override Groq for testing

STYLE_VARIANTS = [
    "professional DSLR photography, golden hour, shallow depth of field, bokeh background",
    "commercial studio photography, clean bright background, sharp focus, high resolution",
    "editorial magazine photography, dramatic natural lighting, crisp detail, vivid colors",
    "luxury brand photography, cinematic lighting, ultra-sharp, rich colors",
    "lifestyle photography, warm natural sunlight, authentic candid feel, 4K",
    "corporate professional photography, polished clean look, high detail, bright",
    "aerial wide-angle photography, crystal clear, dynamic composition, ultra HD",
    "photojournalism style, real-world scene, honest lighting, sharp and detailed",
    "product photography, neutral background, perfect exposure, professional retouch",
    "outdoor event photography, vibrant atmosphere, people in motion, sharp focus",
    "architectural photography, wide perspective, natural light, ultra-detailed",
    "documentary photography, candid real moment, natural colors, high clarity",
]


def _parse_events_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


def _read_cached_csv():
    """Return the last known-good CSV text, or None if there is no usable cache."""
    if not os.path.exists(EVENTS_CACHE_PATH):
        return None
    try:
        with open(EVENTS_CACHE_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"Could not read cache file: {e}")
        return None
    if not text.strip():
        return None
    return text


def _commit_cache_to_repo(text):
    """Push the cache file back to the repo so the next Actions checkout has it."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{EVENTS_CACHE_NAME}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "message": "bot: update cached events sheet",
        "content": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
    }
    existing = requests.get(api_url, headers=headers, timeout=30)
    if existing.status_code == 200:
        payload["sha"] = existing.json()["sha"]
    elif existing.status_code != 404:
        existing.raise_for_status()
    resp = requests.put(api_url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print("Cached events sheet committed to repo")


def _save_cache(text):
    previous = _read_cached_csv()
    if previous == text:
        return
    try:
        with open(EVENTS_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        print(f"Could not write cache file: {e}")
        return
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            _commit_cache_to_repo(text)
        except Exception as e:
            print(f"Could not commit cache to repo ({e}) — local cache still written")


def fetch_events(retries=4, delay=5):
    """Download the events sheet as CSV, retrying on transient network errors.

    Every successful fetch is cached; if the sheet is unreachable we fall back to
    the last known-good copy rather than failing the run.
    """
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.get(SHEET_CSV_URL, timeout=(10, 60))
            response.raise_for_status()
            events = _parse_events_csv(response.text)
            if events:
                _save_cache(response.text)
            else:
                print("Sheet returned no rows — not overwriting cache")
            return events
        except requests.RequestException as e:
            last_error = e
            print(f"Sheet fetch attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                print(f"Waiting {wait}s before retry...")
                time.sleep(wait)

    cached = _read_cached_csv()
    if cached is not None:
        print(f"Sheet unreachable — falling back to cached copy ({EVENTS_CACHE_NAME})")
        return _parse_events_csv(cached)

    raise RuntimeError(
        f"Could not fetch events sheet after {retries} attempts and no cache is available"
    ) from last_error


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
        f"You are a social media manager. Create an engaging post to celebrate or acknowledge this national/international day or observance:\n"
        f"Event: {event_name}\n"
        f"Status: {status}\n\n"
        f"CRITICAL RULES — read carefully:\n"
        f"1. This is a national/international awareness day or holiday. The company is NOT hosting it, NOT selling anything related to it, and NOT organizing any event.\n"
        f"2. NEVER write 'join us', 'come to our', 'visit us', 'shop with us', 'find deals', 'score bargains', or ANY invitation for people to buy or attend something.\n"
        f"3. Write ONLY as a general fun/informative shoutout recognizing the day — like wishing someone a happy holiday.\n"
        f"4. Think of it exactly like posting 'Happy New Year!' — you are just celebrating the day, nothing more.\n\n"
        f"GOOD example for 'National Garage Sale Day':\n"
        f"  caption: 'Happy National Garage Sale Day! 🏡✨ Today is all about the thrill of the hunt — one person's clutter is another's treasure! Did you know garage sales have been a beloved tradition for decades? Whether you love browsing or decluttering, today celebrates the joy of giving items a second life. 🛍️♻️'\n\n"
        f"BAD example (NEVER write like this):\n"
        f"  caption: 'Join us for Garage Sale Day! Come find amazing deals and hidden gems at our sale!' ← WRONG, implies company is hosting\n\n"
        f"Return ONLY a valid JSON object with these exact keys:\n"
        f"  title: a short punchy headline (max 10 words)\n"
        f"  caption: warm friendly post body with emojis (max 150 words)\n"
        f"  hashtags: a string of 10-15 relevant hashtags separated by spaces\n"
        f"  image_prompt: a detailed photorealistic description for an AI image generator — "
        f"describe a real-world professional photography scene (location, objects, environment, lighting, camera angle). "
        f"IMPORTANT: do NOT include any people, faces, or humans in the scene. "
        f"Focus on objects, places, food, nature, architecture, products, or abstract concepts. "
        f"Must feel like a high-quality DSLR photo, not a painting or illustration. "
        f"No text, no logos, no watermarks in the image.\n\n"
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

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


def _enrich_prompt(image_prompt):
    """Append a random professional photography style to keep images varied across runs."""
    style = random.choice(STYLE_VARIANTS)
    return f"{image_prompt}, {style}, photorealistic, hyperrealistic, no people, no faces, no humans, no text, no watermarks"


def _bottom_right_brightness(image_bytes):
    """Return mean brightness (0-255) of the bottom-right quadrant."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    w, h = img.size
    region = img.crop((w // 2, h // 2, w, h))
    return ImageStat.Stat(region).mean[0]


def _overlay_logo(image_bytes):
    """Composite the appropriate logo onto the bottom-right corner."""
    brightness = _bottom_right_brightness(image_bytes)
    # Light background → use dark logo; dark background → use white logo
    logo_path = LOGO_DARK_PATH if brightness > 128 else LOGO_WHITE_PATH
    print(f"Bottom-right brightness: {brightness:.0f} → using {'dark' if brightness > 128 else 'white'} logo")

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size

    logo_width = w // 6  # ~16% of image width
    logo = Image.open(logo_path).convert("RGBA")
    ratio = logo_width / logo.width
    logo = logo.resize((logo_width, int(logo.height * ratio)), Image.LANCZOS)

    padding = 24
    x = w - logo.width - padding
    y = h - logo.height - padding
    img.paste(logo, (x, y), logo)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()


def _upload_to_github(image_bytes):
    """Commit image to the repo's bot-images/ folder and return the raw public URL."""
    filename = f"post_{int(time.time())}.jpg"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/bot-images/{filename}"
    resp = requests.put(
        api_url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "message": "bot: add generated post image",
            "content": base64.b64encode(image_bytes).decode("utf-8"),
        },
        timeout=30,
    )
    resp.raise_for_status()
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/bot-images/{filename}"
    print(f"Hosted on GitHub: {url}")
    return url


def _host_image(image_bytes):
    """Upload image bytes to a public host.
    Tries GitHub (always available in Actions) → imgbb (if key set) → freeimage.host.
    """
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            return _upload_to_github(image_bytes)
        except Exception as e:
            print(f"GitHub upload failed ({e}) — trying imgbb")

    if IMGBB_API_KEY:
        try:
            resp = requests.post(
                "https://api.imgbb.com/1/upload",
                params={"key": IMGBB_API_KEY},
                data={"image": base64.b64encode(image_bytes).decode("utf-8")},
                timeout=30,
            )
            resp.raise_for_status()
            url = resp.json()["data"]["url"]
            print(f"Hosted on imgbb: {url}")
            return url
        except Exception as e:
            print(f"imgbb upload failed ({e}) — trying freeimage.host")

    resp = requests.post(
        "https://freeimage.host/api/1/upload",
        data={
            "key": "6d207e02198a847aa98d0a2a901485a5",
            "source": base64.b64encode(image_bytes).decode("utf-8"),
            "format": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    url = resp.json()["image"]["url"]
    print(f"Hosted on freeimage.host: {url}")
    return url


def _apply_logo(image_bytes):
    """Apply logo overlay and return processed bytes."""
    try:
        processed = _overlay_logo(image_bytes)
        print("Logo overlay applied successfully.")
        return processed
    except Exception as e:
        print(f"WARNING: Logo overlay failed ({e}) — proceeding without logo.")
        return image_bytes


def fetch_image_bytes(event_name, image_prompt):
    """Return image bytes with logo overlaid.
    Priority: Pexels → HF FLUX.1-schnell → Pollinations.
    """
    enriched = _enrich_prompt(image_prompt)
    print(f"Enriched prompt: {enriched}")

    # 1. Pexels — real professional photos, best quality
    if PEXELS_API_KEY:
        try:
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": event_name, "per_page": 15, "orientation": "square"},
                timeout=15,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
            if photos:
                photo = random.choice(photos)
                img_resp = requests.get(photo["src"]["large2x"], timeout=30)
                img_resp.raise_for_status()
                print(f"Pexels image selected: {photo['url']}")
                return _apply_logo(img_resp.content)
            print("Pexels: no results, falling back to HF")
        except Exception as e:
            print(f"Pexels error: {e} — falling back to HF")

    # 2. Hugging Face FLUX.1-schnell
    if HF_TOKEN:
        try:
            print("Generating image with HF FLUX.1-schnell...")
            hf_client = InferenceClient(token=HF_TOKEN)
            pil_img = hf_client.text_to_image(
                enriched,
                model="black-forest-labs/FLUX.1-schnell",
                width=1024,
                height=1024,
                num_inference_steps=4,
            )
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=92)
            print("HF FLUX.1-schnell generation successful.")
            return _apply_logo(buf.getvalue())
        except Exception as e:
            print(f"HF FLUX.1-schnell error: {e} — trying Pollinations")

    # 3. Pollinations
    encoded = urllib.parse.quote(enriched)
    poll_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model=flux&seed={random.randint(0, 99999)}"
    print(f"Fetching Pollinations image...")
    img_resp = requests.get(poll_url, timeout=120)
    img_resp.raise_for_status()
    return _apply_logo(img_resp.content)


def build_facebook_post(title, caption, hashtags):
    return f"{title}\n\n{caption}\n\n{hashtags}"


def build_instagram_caption(title, caption, hashtags):
    return f"{title}\n\n{caption}\n\n{hashtags}"


def post_to_facebook(message, image_bytes):
    """Upload image binary directly to Facebook. Returns API result including photo id."""
    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos"
    response = requests.post(
        url,
        data={"caption": message, "access_token": FB_PAGE_TOKEN},
        files={"source": ("image.jpg", image_bytes, "image/jpeg")},
        timeout=60,
    )
    if not response.ok:
        print(f"Facebook error {response.status_code}: {response.text}")
    response.raise_for_status()
    return response.json()


def get_facebook_photo_url(photo_id):
    """Return the highest-resolution Facebook CDN URL for a photo id."""
    resp = requests.get(
        f"https://graph.facebook.com/v19.0/{photo_id}",
        params={"fields": "images", "access_token": FB_PAGE_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    images = resp.json().get("images", [])
    if images:
        return images[0]["source"]
    return None


def ensure_image_ready(image_url, retries=3, delay=10):
    """Confirm the image URL serves an actual image before posting to Instagram."""
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
        if TEST_IMAGE_PROMPT:
            image_prompt = TEST_IMAGE_PROMPT
            print("TEST_IMAGE_PROMPT override active.")

        print(f"Title: {title}")
        print(f"Caption: {caption}")
        print(f"Hashtags: {hashtags}")
        print(f"Image prompt: {image_prompt}")

        image_bytes = fetch_image_bytes(event_name, image_prompt)

        fb_text = build_facebook_post(title, caption, hashtags)
        fb_result = post_to_facebook(fb_text, image_bytes)
        photo_id = fb_result.get("id")
        print(f"Facebook: posted successfully. Photo ID: {photo_id}")

        if IG_USER_ID:
            ig_caption = build_instagram_caption(title, caption, hashtags)
            image_url = get_facebook_photo_url(photo_id)
            if not image_url:
                print("Could not get Facebook photo URL — falling back to hosted image.")
                image_url = _host_image(image_bytes)
            print(f"Instagram image URL: {image_url}")
            ig_result = post_to_instagram(ig_caption, image_url)
            print(f"Instagram: posted successfully. Post ID: {ig_result.get('id')}")
        else:
            print("Instagram: skipped — INSTAGRAM_USER_ID not configured.")


if __name__ == "__main__":
    main()
