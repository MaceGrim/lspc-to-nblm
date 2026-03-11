"""Debug: extract events from Luma __NEXT_DATA__."""
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

url = "https://lu.ma/ls"
headers = {"User-Agent": "lspc-pipeline/1.0 (podcast automation)"}

resp = requests.get(url, timeout=30, headers=headers)
soup = BeautifulSoup(resp.text, "html.parser")

# Find __NEXT_DATA__
for s in soup.find_all("script"):
    text = s.string or ""
    if "pageProps" in text and "featured_items" in text:
        data = json.loads(text)
        break

page_props = data["props"]["pageProps"]
initial_data = page_props["initialData"]["data"]

# Check structure
print("Keys in initialData.data:", list(initial_data.keys()))

featured = initial_data.get("featured_items", [])
print(f"\nFeatured items: {len(featured)}")

now = datetime.now(timezone.utc)
print(f"Current time UTC: {now.isoformat()}")

for item in featured:
    event = item.get("event", {})
    name = event.get("name", "?")
    start = event.get("start_at", "?")
    url_slug = event.get("url", "?")
    tz = event.get("timezone", "?")

    # Parse start time
    if start and start != "?":
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        is_past = dt < now
    else:
        is_past = None

    has_paper_club = "paper club" in name.lower()

    marker = ""
    if has_paper_club:
        marker = " *** PAPER CLUB ***"

    print(f"\n  Name: {name}{marker}")
    print(f"  Start: {start} (TZ: {tz})")
    print(f"  Past: {is_past}")
    print(f"  URL: https://lu.ma/{url_slug}")

# Also check if there are pagination / more events
print(f"\n\nKeys in page_props:", list(page_props.keys()))
print(f"Keys in initialData:", list(page_props["initialData"].keys()))

# Check for past events section
if "pagination_limit" in str(data):
    print("\nFound pagination_limit in data")

# Check the JSON-LD structured data too
for s in soup.find_all("script", type="application/ld+json"):
    ld = json.loads(s.string)
    if "events" in ld:
        events = ld["events"]
        print(f"\n\nJSON-LD events: {len(events)}")
        for e in events[:5]:
            print(f"  {e.get('name')} | {e.get('startDate')} | {e.get('@id')}")
