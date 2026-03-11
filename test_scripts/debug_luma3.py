"""Debug: check if Luma has API endpoints for past events."""
import requests
import json

headers = {"User-Agent": "lspc-pipeline/1.0 (podcast automation)"}

# Try the Luma API - calendar endpoints
calendar_id = "cal-mc9ZW5C3TzHDv6L"

# Try different API patterns that Luma might expose
endpoints = [
    f"https://api.lu.ma/calendar/get-items?calendar_api_id={calendar_id}&pagination_limit=20",
    f"https://api.lu.ma/calendar/list-events?calendar_api_id={calendar_id}",
    f"https://api.lu.ma/calendar/get-items?calendar_api_id={calendar_id}&period=past&pagination_limit=20",
    f"https://api.lu.ma/url?url=ls",
]

for ep in endpoints:
    print(f"\n--- {ep} ---")
    try:
        resp = requests.get(ep, timeout=10, headers=headers)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            # Show structure
            if isinstance(data, dict):
                print(f"Keys: {list(data.keys())}")
                if "entries" in data:
                    entries = data["entries"]
                    print(f"Entries: {len(entries)}")
                    for e in entries[:3]:
                        if isinstance(e, dict):
                            event = e.get("event", e)
                            print(f"  {event.get('name', '?')} | {event.get('start_at', '?')}")
                elif "events" in data:
                    print(f"Events: {len(data['events'])}")
            else:
                print(str(data)[:500])
        else:
            print(resp.text[:300])
    except Exception as e:
        print(f"Error: {e}")

# Also try the past events page directly
print("\n\n--- Trying /ls/past ---")
resp = requests.get("https://lu.ma/ls/past", timeout=10, headers=headers)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    for s in soup.find_all("script"):
        text = s.string or ""
        if "featured_items" in text or "past" in text.lower()[:100]:
            data = json.loads(text)
            items = data.get("props", {}).get("pageProps", {}).get("initialData", {}).get("data", {}).get("featured_items", [])
            print(f"Featured items: {len(items)}")
            for item in items[:5]:
                event = item.get("event", {})
                print(f"  {event.get('name', '?')} | {event.get('start_at', '?')}")
            break
