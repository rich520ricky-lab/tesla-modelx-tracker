#!/usr/bin/env python3
"""
Tesla Model X FUSC Tracker — Craigslist LA Scraper
Scrapes Los Angeles Craigslist for used Tesla Model X listings
and detects Free Unlimited Supercharging (FUSC) in the listing text.
"""

import re
import json
import html
import ssl
import time
import os
import sys
from datetime import datetime
from urllib.request import urlopen, Request

# ── Config ──────────────────────────────────────────────────────────────
BASE_URL = "https://losangeles.craigslist.org"
SEARCH_URL = (
    f"{BASE_URL}/search/cta"
    "?query=tesla+model+x"
    "&sort=date"
    "&srchType=T"       # title only
    "&hasPic=1"
    "&bundleDuplicates=1"
)
DATA_FILE = "listings.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Keywords that indicate Free Unlimited Supercharging
FUSC_KEYWORDS = [
    "free supercharging",
    "free unlimited supercharging",
    "fusc",
    "lifetime supercharging",
    "unlimited supercharging",
]

# Phrases that explicitly deny FUSC
FUSC_DENY_PHRASES = [
    "does not have free supercharging",
    "no free supercharging",
    "does not include supercharging",
    "not included",
    "does not have supercharging",
    "does not come with supercharging",
]

# ── Helpers ─────────────────────────────────────────────────────────────

def fetch_url(url):
    """Fetch a URL and return HTML string."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    with urlopen(req, context=ctx, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")

def extract_listings_from_search(html_text):
    """Parse search results page and return list of listing dicts."""
    listings = []
    
    # Parse the cl-static-search-result items which contain title, link, and price
    # Pattern: <li class="cl-static-search-result" title="TITLE">
    #   <a href="URL">
    #     <div class="title">TITLE</div>
    #     <div class="price">$PRICE</div>
    #     ...
    #   </a>
    # </li>
    
    item_pattern = (
        r'<li\s+class="cl-static-search-result"[^>]*'
        r'title="([^"]*)"[^>]*>'                          # title attr
        r'\s*<a\s+href="([^"]+)"'                         # link
        r'(?:[^>]*)>.*?'                                  # open a
        r'<div\s+class="price"[^>]*>\s*\$?([^<]+)\s*<'    # price
        r'.*?</li>'                                        # close
    )
    
    items = re.findall(item_pattern, html_text, re.DOTALL)
    
    seen_ids = set()
    
    for title_raw, url, price_raw in items:
        title = html.unescape(title_raw.strip())
        url_clean = html.unescape(url.strip())
        
        # Only process Tesla Model X listings
        if "tesla" not in title.lower() or "model" not in title.lower():
            continue
        
        post_id_match = re.search(r'/(\d+)\.html$', url_clean)
        post_id = post_id_match.group(1) if post_id_match else ""
        
        if post_id in seen_ids or not post_id:
            continue
        seen_ids.add(post_id)
        
        # Clean price
        price = price_raw.strip()
        if not price.startswith("$"):
            price = f"${price}"
        
        # Seller type from URL path
        seller_type = "unknown"
        if "/cto/" in url_clean:
            seller_type = "owner"
        elif "/ctd/" in url_clean:
            seller_type = "dealer"
        
        listings.append({
            "id": post_id,
            "title": title,
            "price": price,
            "url": url_clean,
            "seller_type": seller_type,
            "posted": "",
            "mileage": "",
            "location": "",
            "description": "",
            "fusc": "unknown",
        })
    
    return listings

def fetch_listing_detail(listing):
    """Fetch individual listing page and extract details."""
    try:
        html_text = fetch_url(listing["url"])
    except Exception as e:
        print(f"  ⚠️  Error fetching {listing['url']}: {e}")
        return listing
    
    # Extract description body
    # Craigslist description is in a <section> with id="postingbody" or in a <div>
    desc_match = re.search(
        r'<div\s+class="[^"]*postingbody[^"]*"[^>]*>'
        r'(.*?)</div>\s*<ul',
        html_text, re.DOTALL
    )
    if not desc_match:
        # Try alternative patterns
        desc_match = re.search(
            r'<section\s+id="postingbody"[^>]*>'
            r'(.*?)</section',
            html_text, re.DOTALL
        )
    
    if desc_match:
        desc_raw = desc_match.group(1)
        # Clean HTML tags
        desc = re.sub(r'<[^>]+>', ' ', desc_raw)
        desc = html.unescape(desc)
        desc = re.sub(r'\s+', ' ', desc).strip()
        listing["description"] = desc
    else:
        listing["description"] = ""
    
    # Extract posted date
    posted_match = re.search(
        r'datetime="([^"]*)"[^>]*>\s*(.*?)\s*</time',
        html_text, re.DOTALL
    )
    if posted_match:
        listing["posted"] = posted_match.group(2).strip()
    else:
        # Try simpler pattern
        posted_match = re.search(
            r'<time[^>]*>\s*(.*?)\s*</time>',
            html_text, re.DOTALL
        )
        if posted_match:
            listing["posted"] = posted_match.group(1).strip()
    
    # Extract mileage
    mile_match = re.search(r'odometer:\s*([0-9,]+)', html_text, re.IGNORECASE)
    if mile_match:
        listing["mileage"] = mile_match.group(1)
    
    # Extract location
    loc_match = re.search(r'"(?:Los\s+Angeles|location):?\s*([^"]+)"', html_text, re.IGNORECASE)
    if not loc_match:
        # Try from the title/h1
        loc_match = re.search(
            r'<h1[^>]*>\s*[^<]+\$[0-9,]+\s*\(([^)]+)\)',
            html_text
        )
    if loc_match:
        listing["location"] = loc_match.group(1).strip()
    
    return listing

def check_fusc(listing):
    """
    Check if a listing has Free Unlimited Supercharging.
    Uses keywords + year heuristic:
    - Pre-2020 Model X originally came with FUSC unless explicitly denied
    - 2020+ Model X may still have it if mentioned explicitly
    
    Returns: 'yes', 'no', or 'unknown'
    """
    title = listing.get("title", "")
    desc = listing.get("description", "")
    text = (title + " " + desc).lower()
    
    # First check for denial phrases
    for phrase in FUSC_DENY_PHRASES:
        if phrase in text:
            return "no"
    
    # Check for explicit FUSC keywords
    for kw in FUSC_KEYWORDS:
        if kw in text:
            return "yes"
    
    # Check "supercharger" or "supercharging" in description context
    # "Full access to supercharger" != free supercharging
    # Only count if it mentions "free" or "unlimited" near "supercharg"
    if re.search(r'(free|unlimited|lifetime)\s+\w*\s*supercharg', text):
        return "yes"
    
    # Year heuristic: extract year from title
    year_match = re.search(r'(20\d\d)', title)
    if year_match:
        year = int(year_match.group(1))
        # Pre-2020 Model X originally had FUSC
        # Unless explicitly denied (already checked above)
        if year <= 2019:
            return "yes"  # Assume yes unless explicitly stated otherwise
        # 2020 is a transitional year - some have FUSC, some don't
        # Return unknown so the listing appears for manual review
        if year == 2020:
            return "unknown"
        # 2021+ almost certainly doesn't have FUSC
        return "no"
    
    return "unknown"

def merge_listings(existing, new_listings):
    """Merge new listings with existing ones, keeping existing descriptions."""
    existing_by_id = {l["id"]: l for l in existing}
    
    added = 0
    for nl in new_listings:
        if nl["id"] not in existing_by_id:
            existing_by_id[nl["id"]] = nl
            added += 1
        else:
            # Update fields that might have changed (price, posted)
            old = existing_by_id[nl["id"]]
            if nl.get("price") and nl["price"] != old.get("price"):
                old["price"] = nl["price"]
            if nl.get("description") and not old.get("description"):
                old["description"] = nl["description"]
            if nl.get("mileage") and not old.get("mileage"):
                old["mileage"] = nl["mileage"]
            if nl.get("posted") and not old.get("posted"):
                old["posted"] = nl["posted"]
    
    return list(existing_by_id.values()), added

def run():
    """Main scraping pipeline."""
    print("🚗 Tesla Model X FUSC Tracker")
    print("=" * 50)
    
    # Load existing data
    existing = []
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                existing_data = json.load(f)
                existing = existing_data.get("listings", [])
                print(f"📂 Loaded {len(existing)} existing listings")
        except Exception as e:
            print(f"⚠️  Could not load existing data: {e}")
    
    # Step 1: Search
    print(f"\n🔍 Searching Craigslist LA for Tesla Model X...")
    try:
        search_html = fetch_url(SEARCH_URL)
    except Exception as e:
        print(f"❌ Search failed: {e}")
        return 1
    
    new_listings = extract_listings_from_search(search_html)
    print(f"📋 Found {len(new_listings)} listings in search results")
    
    # Step 2: Fetch individual listing details (only for new or unknown ones)
    to_fetch = [l for l in new_listings if l["id"] not in {e["id"] for e in existing}]
    print(f"\n📄 Fetching details for {len(to_fetch)} new listings...")
    
    for i, listing in enumerate(to_fetch):
        print(f"  {i+1}/{len(to_fetch)}: {listing['title'][:50]}...")
        listing = fetch_listing_detail(listing)
        # Rate limiting
        if i < len(to_fetch) - 1:
            time.sleep(1.5)
    
    # Merge
    merged, added = merge_listings(existing, new_listings)
    
    # Step 3: Check FUSC status for all listings
    fusc_yes = 0
    fusc_unknown = 0
    fusc_no = 0
    for listing in merged:
        fusc = check_fusc(listing)
        listing["fusc"] = fusc
        if fusc == "yes":
            fusc_yes += 1
        elif fusc == "unknown":
            fusc_unknown += 1
        else:
            fusc_no += 1
    
    # Step 4: Sort by posted date (newest first)
    merged.sort(key=lambda l: l.get("posted", ""), reverse=True)
    
    # Step 5: Save
    last_updated = datetime.now().strftime("%B %d, %Y at %I:%M %p %Z")
    output = {
        "last_updated": last_updated,
        "search_url": SEARCH_URL,
        "sources": ["craigslist Los Angeles"],
        "listings": merged,
    }
    
    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ Done! Updated {DATA_FILE}")
    print(f"   Total listings: {len(merged)}")
    print(f"   New this run: {added}")
    print(f"   ⚡ FUSC confirmed: {fusc_yes}")
    print(f"   ❓ FUSC unknown: {fusc_unknown}")
    print(f"   ❌ No FUSC: {fusc_no}")
    print(f"   Last updated: {last_updated}")
    
    # Print FUSC listings for immediate view
    if fusc_yes > 0:
        print(f"\n⚡ FUSC CONFIRMED LISTINGS:")
        print("-" * 50)
        for l in merged:
            if l.get("fusc") == "yes":
                print(f"  • {l['title']} — {l['price']} | {l.get('mileage','?')} mi")
                print(f"    {l['url']}")
    
    return 0

if __name__ == "__main__":
    sys.exit(run())