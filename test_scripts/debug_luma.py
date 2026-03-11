"""Debug: see what Luma returns for the calendar page."""
import requests
from bs4 import BeautifulSoup
import json

url = "https://lu.ma/ls"
headers = {"User-Agent": "lspc-pipeline/1.0 (podcast automation)"}

print(f"Fetching {url}...")
resp = requests.get(url, timeout=30, headers=headers)
print(f"Status: {resp.status_code}")
print(f"Content-Type: {resp.headers.get('content-type')}")
print(f"Content length: {len(resp.text)} chars")

soup = BeautifulSoup(resp.text, "html.parser")

# Check for JSON data in script tags
scripts = soup.find_all("script")
print(f"\nFound {len(scripts)} <script> tags")
for i, s in enumerate(scripts):
    text = s.string or ""
    if "paper" in text.lower() or "club" in text.lower() or "event" in text.lower():
        print(f"\n--- Script #{i} (contains event/paper/club) ---")
        print(text[:2000])
        print("...")

# Check for event cards / links
links = soup.find_all("a", href=True)
print(f"\nFound {len(links)} links")
luma_links = [l for l in links if "lu.ma" in l.get("href", "")]
print(f"Luma links: {len(luma_links)}")
for l in luma_links[:20]:
    title = l.get_text(strip=True)[:80]
    href = l["href"]
    print(f"  {href} -> {title}")

# Check page title
title = soup.find("title")
print(f"\nPage title: {title.string if title else 'none'}")

# Look for __NEXT_DATA__ or similar
for s in scripts:
    text = s.string or ""
    if "__NEXT_DATA__" in text or "pageProps" in text:
        print(f"\n--- Found __NEXT_DATA__ (first 3000 chars) ---")
        print(text[:3000])
        break

# Check for any data attributes
divs_with_data = soup.find_all(attrs={"data-event": True})
print(f"\nElements with data-event: {len(divs_with_data)}")

# Check for any time elements
times = soup.find_all("time")
print(f"Time elements: {len(times)}")
for t in times[:10]:
    print(f"  {t.get('datetime', 'no datetime')} -> {t.get_text(strip=True)[:60]}")
