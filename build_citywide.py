"""Prepare Boston-wide restaurant data for the StoryMap.

Runs on your laptop, separately from the Boston Deals pipeline. Produces:
  data/citywide_restaurants.csv      every licensed restaurant, with flags
  data/citywide_public.csv           only places open to the public (use this in ArcGIS)
  data/flagged_institutional.csv     places flagged as not open to the public, for review
"""
import csv
import math
import os
import re
import time
from collections import Counter, defaultdict

import requests
from rapidfuzz import fuzz

# ---------- Settings ----------
RESOURCE_ID = "f1e13724-284d-478c-b8bc-ef042aa5b70b"          # same Boston food-license resource as the deals project
KEEP_CATEGORIES = {"Eating & Drinking", "Eating & Drinking w/ Take Out"}          # copy the same set you use in the deals project
FOCUS_ZIPS = {"02116", "02199", "02215", "02115"}  # Back Bay + Fenway (where deals were tracked)
CHAIN_MIN_LOCATIONS = 3                          # same name at 3+ addresses = chain
MATCH_METERS = 75                                # OpenStreetMap match must be this close...
MATCH_NAME_SCORE = 70                            # ...and this similar by name
COMPETITION_METERS = 200                         # "nearby competitors" radius
OUT_DIR = "data"
DEALS_FILE = os.path.join(OUT_DIR, "deals_bbf.csv")   # optional, exported from Supabase (Step 3)

LICENSE_API = "https://data.boston.gov/api/3/action/datastore_search"
BOSTON_BBOX = (42.227, -71.191, 42.397, -70.986)      # south, west, north, east
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
HEADERS = {"User-Agent": "boston-food-geography/0.1 (taybos4811@gmail.com)"}

# ---------- Places not open to the public ----------
SKIP_PATTERNS = [
    r"universit", r"\bcollege\b", r"\bschool\b", r"\bacademy\b", r"\bcampus\b", r"dining hall",
    r"residence hall", r"\bdorm", r"\bhospital\b", r"\bmedical\b", r"\bclinic\b", r"health care",
    r"\bhotel\b", r"\bmarriott\b", r"\bhilton\b", r"\bsheraton\b", r"\bwestin\b", r"\bhyatt\b",
    r"four seasons", r"\bfairmont\b", r"symphony hall", r"concert hall", r"cafeteria", r"\bkiosk\b",
    r"concession", r"\bportable\b", r"\bcart\b", r"\bemployee", r"\bstaff\b", r"\bfloor\b", r"\bfl\b",
    r"\d+(st|nd|rd|th) fl", r"\baramark\b", r"\bsodexo\b", r"compass group", r"\blevy\b",
]
KEEP_PATTERNS = [r"pizza", r"\btea\b", r"bakery", r"\bgrill\b", r"kitchen", r"\btaqueria\b", r"\bdeli\b",
                 r"house of", r"eataly", r"\blicks\b"]
SKIP_RE = re.compile("|".join(SKIP_PATTERNS), re.I)
KEEP_RE = re.compile("|".join(KEEP_PATTERNS), re.I)

# ---------- Food categories ----------
# OpenStreetMap cuisine words -> your categories (first match wins)
CUISINE_RULES = [
    (r"\bcoffee|\bbubble_tea|\btea\b", "Café"),            # \b so "steak" doesn't match "tea"
    (r"pizza", "Pizza"),
    (r"ice_cream|dessert|frozen_yogurt|donut|cake|crepe|chocolate|pastry", "Bakery & dessert"),
    (r"burger|sandwich|hot_dog|bagel|deli|sub", "Burgers & sandwiches"),
    (r"mexican|tex-mex|taco|burrito|latin|brazilian|peruvian|colombian|salvadoran|cuban|dominican|caribbean",
     "Mexican & Latin"),
    (r"chinese|japanese|sushi|ramen|korean|thai|vietnamese|asian|dumpling|noodle|taiwanese|poke", "Asian"),
    (r"indian|nepalese|pakistani|bangladeshi|himalayan", "Indian & South Asian"),
    (r"mediterranean|greek|middle_eastern|lebanese|turkish|falafel|kebab|persian|moroccan|ethiopian",
     "Mediterranean & Middle Eastern"),
    (r"italian|pasta", "Italian"),
    (r"seafood|fish|oyster|lobster", "Seafood"),
    (r"american|bbq|barbecue|steak|diner|chicken|wings|breakfast|brunch|regional", "American"),
]
# Restaurant names -> categories, used when OpenStreetMap has nothing specific
NAME_RULES = [
    (r"pizz", "Pizza"),
    (r"bakery|bakehouse|patisserie|p[aâ]tisserie|donut|doughnut|cupcake|gelato|ice cream|creamery|dessert|cookie|"
     r"crepe|churro", "Bakery & dessert"),
    (r"\bcaf[eé]\b|coffee|espresso|roaster|\btea\b|boba", "Café"),
    (r"taqueria|\btaco|burrito|cantina|mexican|latin|empanada|arepa|pupusa", "Mexican & Latin"),
    (r"sushi|ramen|noodle|\bpho\b|thai|chinese|szechuan|sichuan|dumpling|korean|\bwok\b|\bpoke\b|asian|"
     r"hibachi|teriyaki|bao\b", "Asian"),
    (r"indian|curry|tandoor|masala|biryani|himalayan|nepal", "Indian & South Asian"),
    (r"falafel|shawarma|kebab|kabob|gyro|mediterranean|greek|lebanese|halal|hummus", "Mediterranean & Middle Eastern"),
    (r"trattoria|pasta|italian|osteria|ristorante|enoteca", "Italian"),
    (r"oyster|lobster|seafood|\bfish\b|clam|crab", "Seafood"),
    (r"burger|sandwich|\bsubs?\b|\bdeli\b|bagel|hot dog", "Burgers & sandwiches"),
    (r"\bpub\b|tavern|brewery|brewing|\bbar\b|saloon|taproom|beer|lounge", "Bar & pub"),
    (r"diner|\bbbq\b|barbecue|steakhouse|chicken|wings|smokehouse", "American"),
]
AMENITY_DEFAULTS = {"cafe": "Café", "ice_cream": "Bakery & dessert", "bar": "Bar & pub", "pub": "Bar & pub",
                    "fast_food": "Quick bites", "food_court": "Quick bites"}
SHOP_DEFAULTS = {"bakery": "Bakery & dessert", "pastry": "Bakery & dessert", "confectionery": "Bakery & dessert",
                 "coffee": "Café", "deli": "Burgers & sandwiches"}

STOPWORDS = {"the", "inc", "llc", "corp", "co", "company", "ltd", "dba", "group"}


# ---------- Helpers ----------
def norm_name(s):
    s = re.sub(r"#\s*\d+", " ", (s or "").lower())       # drop store numbers like "#1234"
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(w for w in s.split() if w not in STOPWORDS)

def norm_zip(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:5].zfill(5) if digits else ""

def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def license_key(r):
    """Same key as the deals project, so deal stats can be joined."""
    return "|".join(str(r.get(f) or "").strip().lower() for f in ("businessname", "address", "license_add_dt_tm"))

def meters(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

class Grid:
    """Buckets points into ~100 m cells so 'what's nearby' checks stay fast."""
    CELL = 0.001
    def __init__(self):
        self.cells = defaultdict(list)
    def add(self, lat, lon, item):
        self.cells[(int(lat / self.CELL), int(lon / self.CELL))].append((lat, lon, item))
    def near(self, lat, lon, radius_m):
        dlat = math.ceil(radius_m / 111.0) + 1
        dlon = math.ceil(radius_m / (111.0 * math.cos(math.radians(lat)))) + 1
        ci, cj = int(lat / self.CELL), int(lon / self.CELL)
        for i in range(ci - dlat, ci + dlat + 1):
            for j in range(cj - dlon, cj + dlon + 1):
                for plat, plon, item in self.cells.get((i, j), []):
                    if meters(lat, lon, plat, plon) <= radius_m:
                        yield item

def first_rule(text, rules):
    for pattern, category in rules:
        if re.search(pattern, text or "", re.I):
            return category
    return None


# ---------- Step 1: licenses ----------
def fetch_licenses():
    records, offset = [], 0
    while True:
        r = requests.get(LICENSE_API, params={"resource_id": RESOURCE_ID, "limit": 1000, "offset": offset},
                         headers=HEADERS, timeout=60)
        r.raise_for_status()
        batch = r.json()["result"]["records"]
        if not batch:
            return records
        records.extend(batch)
        offset += len(batch)

def active_restaurants(records):
    seen, rows = set(), []
    for r in records:
        if str(r.get("licstatus", "")).strip().lower() != "active":
            continue
        if r.get("descript") not in KEEP_CATEGORIES:
            continue
        lat, lon = to_float(r.get("latitude")), to_float(r.get("longitude"))
        if lat is None or lon is None:
            continue
        sign = r.get("dbaname") or r.get("businessname") or ""
        dedupe = (norm_name(sign), str(r.get("address") or "").strip().lower())
        if dedupe in seen:
            continue                       # same place with more than one license
        seen.add(dedupe)
        rows.append({"license_key": license_key(r), "name": sign, "operator": r.get("businessname") or "",
                     "address": r.get("address") or "", "zip": norm_zip(r.get("zip")), "lat": lat, "lon": lon})
    return rows


# ---------- Step 2: OpenStreetMap ----------
def fetch_osm():
    s, w, n, e = BOSTON_BBOX
    query = f"""
[out:json][timeout:180];
(
  nwr["amenity"~"^(restaurant|cafe|fast_food|bar|pub|ice_cream|food_court)$"]({s},{w},{n},{e});
  nwr["shop"~"^(bakery|pastry|coffee|deli|confectionery)$"]({s},{w},{n},{e});
);
out center tags;
"""
    for attempt in range(1, 4):
        for url in OVERPASS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=300)
                if r.status_code == 200:
                    return r.json()["elements"]
                print(f"{url}: HTTP {r.status_code}")
            except (requests.RequestException, ValueError) as ex:
                print(f"{url}: {ex}")
        print(f"All Overpass servers busy — retrying in {60 * attempt} seconds...")
        time.sleep(60 * attempt)
    raise RuntimeError("OpenStreetMap servers are busy; try again later.")

def osm_points(elements):
    points = []
    for el in elements:
        tags = el.get("tags", {})
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lon = el.get("lon", el.get("center", {}).get("lon"))
        if lat is not None and lon is not None and tags.get("name"):
            points.append({"lat": lat, "lon": lon, "tags": tags, "norm": norm_name(tags["name"])})
    return points

def osm_category(tags):
    """A specific category from OpenStreetMap, or None if it only says 'restaurant'."""
    amenity, shop = tags.get("amenity"), tags.get("shop")
    cuisine = (tags.get("cuisine") or "").lower()
    if shop in SHOP_DEFAULTS:
        return SHOP_DEFAULTS[shop]
    if amenity in ("cafe", "ice_cream", "bar", "pub"):
        return AMENITY_DEFAULTS[amenity]
    from_cuisine = first_rule(cuisine, CUISINE_RULES)
    if from_cuisine:
        return from_cuisine
    return AMENITY_DEFAULTS.get(amenity)     # fast_food / food_court -> Quick bites; restaurant -> None


# ---------- Main ----------
def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Downloading Boston food licenses...")
    rows = active_restaurants(fetch_licenses())
    print(f"{len(rows)} active, licensed restaurants")

    # Places not open to the public
    for row in rows:
        skip = SKIP_RE.search(f"{row['name']} | {row['operator']}")
        keep = KEEP_RE.search(row["name"]) if skip else None
        row["is_institutional"] = int(bool(skip and not keep))
        row["institutional_reason"] = f"matched '{skip.group(0)}'" if row["is_institutional"] else ""

    # Chains: same cleaned name at CHAIN_MIN_LOCATIONS+ public addresses
    counts = Counter(norm_name(r["name"]) for r in rows if not r["is_institutional"])
    for row in rows:
        n = counts.get(norm_name(row["name"]), 0)
        row["chain_locations"] = n if not row["is_institutional"] else 0
        row["is_chain"] = int(not row["is_institutional"] and n >= CHAIN_MIN_LOCATIONS)

    # OpenStreetMap match -> category
    print("Downloading OpenStreetMap food places...")
    osm = osm_points(fetch_osm())
    print(f"{len(osm)} OpenStreetMap places")
    osm_grid = Grid()
    for p in osm:
        osm_grid.add(p["lat"], p["lon"], p)

    for row in rows:
        target = norm_name(row["name"])
        best, best_score = None, 0
        for p in osm_grid.near(row["lat"], row["lon"], MATCH_METERS):
            score = fuzz.token_set_ratio(target, p["norm"])
            if score > best_score:
                best, best_score = p, score
        matched = best if best_score >= MATCH_NAME_SCORE else None
        category = osm_category(matched["tags"]) if matched else None
        source = "osm" if category else ""
        if not category:
            category = first_rule(row["name"], NAME_RULES)
            source = "name" if category else ""
        if not category and matched:
            category, source = "Other restaurant", "osm"
        if not category:
            category, source = "Unknown", "unknown"
        row["category"], row["category_source"] = category, source
        row["osm_matched"] = int(matched is not None)

    # Competition: nearby public restaurants, and nearby ones of the same category
    public = [r for r in rows if not r["is_institutional"]]
    pub_grid = Grid()
    for r in public:
        pub_grid.add(r["lat"], r["lon"], r)
    for row in rows:
        if row["is_institutional"]:
            row["competitors_200m"] = row["same_category_200m"] = ""
            continue
        near = [r for r in pub_grid.near(row["lat"], row["lon"], COMPETITION_METERS) if r is not row]
        row["competitors_200m"] = len(near)
        row["same_category_200m"] = (sum(1 for r in near if r["category"] == row["category"])
                                     if row["category"] not in ("Unknown", "Other restaurant") else "")

    # Focus area + deal stats (optional file from Supabase)
    deals = {}
    if os.path.exists(DEALS_FILE):
        with open(DEALS_FILE, newline="", encoding="utf-8") as f:
            for d in csv.DictReader(f):
                deals[d["license_key"].strip().lower()] = d
        print(f"Deal stats loaded for {len(deals)} Back Bay/Fenway restaurants")
    else:
        print(f"No {DEALS_FILE} yet — deal columns left empty (see Step 3 of the guide)")
    for row in rows:
        row["focus_area"] = int(row["zip"] in FOCUS_ZIPS)
        d = deals.get(row["license_key"])
        row["deals_tracked"] = int(d is not None)
        row["deals_30d"] = int(d["deals_30d"] or 0) if d else ""
        row["has_happy_hour"] = int(str(d["has_happy_hour"]).lower() in ("true", "t", "1")) if d else ""
        row["main_deal_type"] = (d.get("main_deal_type") or "") if d else ""

    # Save
    columns = ["license_key", "name", "operator", "address", "zip", "lat", "lon", "focus_area",
               "is_institutional", "institutional_reason", "is_chain", "chain_locations",
               "category", "category_source", "osm_matched", "competitors_200m", "same_category_200m",
               "deals_tracked", "deals_30d", "has_happy_hour", "main_deal_type"]
    def write(path, data):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)
    write(os.path.join(OUT_DIR, "citywide_restaurants.csv"), rows)
    write(os.path.join(OUT_DIR, "citywide_public.csv"), public)
    write(os.path.join(OUT_DIR, "flagged_institutional.csv"), [r for r in rows if r["is_institutional"]])

    # Summary
    n_pub = len(public)
    src = Counter(r["category_source"] for r in public)
    chains = Counter(norm_name(r["name"]) for r in public if r["is_chain"])
    print("\n----- Summary -----")
    print(f"{len(rows)} licensed restaurants · {len(rows) - n_pub} flagged as not open to the public")
    print(f"Categories (public places): {100 * src['osm'] / n_pub:.0f}% OpenStreetMap, "
          f"{100 * src['name'] / n_pub:.0f}% from names, {100 * src['unknown'] / n_pub:.0f}% unknown")
    print(f"Chains ({CHAIN_MIN_LOCATIONS}+ locations): {sum(chains.values())} restaurants across {len(chains)} chains")
    print("Biggest chains:", ", ".join(f"{name} ({n})" for name, n in chains.most_common(8)))
    print("Categories:", dict(Counter(r["category"] for r in public).most_common()))
    focus = [r for r in public if r["focus_area"]]
    print(f"Back Bay/Fenway: {len(focus)} public restaurants, "
          f"{sum(r['deals_tracked'] == 1 for r in focus)} with deal stats")
    print(f"\nFiles written to {OUT_DIR}/. Review flagged_institutional.csv before uploading.")

if __name__ == "__main__":
    main()