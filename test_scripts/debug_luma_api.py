"""Debug: explore Luma API structure for past events."""
import requests
import json

headers = {"User-Agent": "lspc-pipeline/1.0 (podcast automation)"}

# Get past events with full detail
resp = requests.get(
    "https://api.lu.ma/calendar/get-items",
    params={
        "calendar_api_id": "cal-mc9ZW5C3TzHDv6L",
        "period": "past",
        "pagination_limit": 5,
    },
    headers=headers,
    timeout=30,
)
data = resp.json()

print("Top-level keys:", list(data.keys()))
print(f"Entries: {len(data['entries'])}")
print(f"Has more: {data.get('has_more')}")
print(f"Next cursor: {data.get('next_cursor')}")

# Look at first entry structure
entry = data["entries"][0]
print(f"\nEntry keys: {list(entry.keys())}")
event = entry.get("event", {})
print(f"Event keys: {list(event.keys())}")

print(f"\nFirst event detail:")
print(json.dumps(entry, indent=2)[:3000])

# Now fetch the event page directly to see what paper URLs we get
event_slug = event.get("url", "")
event_url = f"https://lu.ma/{event_slug}"
print(f"\n\n--- Fetching event page: {event_url} ---")
resp2 = requests.get(event_url, timeout=30, headers=headers)
print(f"Status: {resp2.status_code}")

from bs4 import BeautifulSoup
soup = BeautifulSoup(resp2.text, "html.parser")
links = soup.find_all("a", href=True)
print(f"Links on page: {len(links)}")
for l in links:
    href = l["href"]
    if "arxiv" in href.lower() or ".pdf" in href.lower():
        print(f"  PAPER: {href}")
    elif not href.startswith(("#", "/")):
        text = l.get_text(strip=True)[:60]
        if text and "lu.ma" not in href and "luma" not in href:
            print(f"  LINK:  {href} -> {text}")

# Also check if the API has a way to get calendar_api_id from the slug
print("\n\n--- Getting calendar ID from slug ---")
resp3 = requests.get("https://api.lu.ma/url?url=ls", timeout=10, headers=headers)
url_data = resp3.json()
print(f"Keys: {list(url_data.keys())}")
print(f"Kind: {url_data.get('kind')}")
if "data" in url_data:
    d = url_data["data"]
    if isinstance(d, dict):
        print(f"Data keys: {list(d.keys())}")
        cal = d.get("calendar", {})
        print(f"Calendar api_id: {cal.get('api_id')}")
        print(f"Calendar slug: {cal.get('slug')}")
