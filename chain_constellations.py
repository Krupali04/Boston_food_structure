"""Chain constellations: connect every location of the same chain brand with glowing lines.

Reads  data/citywide_public_t.csv (or data/citywide_public.csv)
Writes data/chain_lines.geojson   one line per connection, with brand, locations, length
       data/chain_stars.csv       every location of each brand, as points
       data/chain_brands.csv      one row per brand: locations, constellation length, center point (for labels)

Lines are a minimum spanning tree per brand: the shortest set of lines that links all of a
brand's locations, with no loops. Each store ends up linked to its nearest sister stores.
"""
import csv
import json
import math
import os
import re
from collections import defaultdict

IN_FILES = ["data/citywide_public_t.csv", "data/citywide_public.csv"]   # first one that exists is used
OUT_DIR = "data"
MIN_LOCATIONS = 5          # brands with fewer locations are left out

# Spelling variants -> one brand name (first match wins). Add more as you spot them.
BRAND_RULES = [
    (r"dunkin", "Dunkin'"), (r"starbucks", "Starbucks"), (r"mc ?donald", "McDonald's"),
    (r"\bsubway\b", "Subway"), (r"flour bakery", "Flour Bakery"), (r"\btatte\b", "Tatte"),
    (r"caff?e nero", "Caffè Nero"), (r"panera", "Panera"), (r"blank street", "Blank Street"),
    (r"sweetgreen", "Sweetgreen"), (r"domino", "Domino's"), (r"pizza hut", "Pizza Hut"),
    (r"five guys", "Five Guys"), (r"chipotle", "Chipotle"), (r"\bcava\b", "Cava"),
    (r"gong ?cha", "Gong Cha"), (r"ben (&|and) jerry", "Ben & Jerry's"), (r"insomnia cookies", "Insomnia Cookies"),
    (r"taco bell", "Taco Bell"), (r"burger king", "Burger King"), (r"wendy", "Wendy's"),
    (r"popeye", "Popeyes"), (r"\bkfc\b|kentucky fried", "KFC"), (r"chick-?fil-?a", "Chick-fil-A"),
    (r"jersey mike", "Jersey Mike's"), (r"pret a manger", "Pret A Manger"), (r"au bon pain", "Au Bon Pain"),
    (r"pavement", "Pavement Coffeehouse"), (r"tropical smoothie", "Tropical Smoothie Cafe"),
    (r"\bcosi\b", "Cosi"), (r"boloco", "Boloco"), (r"clover", "Clover Food Lab"),
]
STOPWORDS = {"the", "inc", "llc", "corp", "co", "company", "ltd", "dba", "group"}


def meters(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def brand_of(name):
    """One clean brand name per chain, so spelling variants and store numbers don't split brands."""
    raw = (name or "").lower()
    for pattern, brand in BRAND_RULES:
        if re.search(pattern, raw):
            return brand
    cleaned = re.sub(r"#\s*\d+", " ", raw)
    cleaned = re.sub(r"[^a-z0-9& ]", " ", cleaned)
    words = [w for w in cleaned.split() if w not in STOPWORDS]
    return " ".join(words).title() if words else None

def spanning_tree(points):
    """Prim's algorithm: returns (i, j, length_m) pairs linking all points with the shortest total length."""
    n = len(points)
    in_tree = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0
    edges = []
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        if parent[u] >= 0:
            edges.append((parent[u], u, best[u]))
        for v in range(n):
            if not in_tree[v]:
                d = meters(points[u]["lat"], points[u]["lon"], points[v]["lat"], points[v]["lon"])
                if d < best[v]:
                    best[v], parent[v] = d, u
    return edges

def main():
    in_file = next((f for f in IN_FILES if os.path.exists(f)), None)
    if not in_file:
        raise SystemExit("No input found: run build_citywide.py first.")
    with open(in_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Read {len(rows)} restaurants from {in_file}")

    brands = defaultdict(list)
    for r in rows:
        b = brand_of(r["name"])
        if b:
            brands[b].append({"name": r["name"], "address": r.get("address", ""),
                              "lat": float(r["lat"]), "lon": float(r["lon"]),
                              "dist_to_t_m": r.get("dist_to_t_m", "")})
    brands = {b: pts for b, pts in brands.items() if len(pts) >= MIN_LOCATIONS}
    print(f"{len(brands)} brands with {MIN_LOCATIONS}+ locations")

    features, stars, summary = [], [], []
    for brand, pts in sorted(brands.items(), key=lambda kv: -len(kv[1])):
        edges = spanning_tree(pts)
        total = sum(e[2] for e in edges)
        for k, (i, j, d) in enumerate(edges):
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[pts[i]["lon"], pts[i]["lat"]], [pts[j]["lon"], pts[j]["lat"]]]},
                "properties": {"brand": brand, "locations": len(pts), "length_m": round(d), "segment": k + 1},
            })
        for p in pts:
            stars.append({"brand": brand, "name": p["name"], "address": p["address"],
                          "lat": p["lat"], "lon": p["lon"], "locations": len(pts)})
        dists = [float(p["dist_to_t_m"]) for p in pts if p["dist_to_t_m"] not in ("", None)]
        summary.append({
            "brand": brand, "locations": len(pts), "constellation_km": round(total / 1000, 1),
            "center_lat": round(sum(p["lat"] for p in pts) / len(pts), 6),
            "center_lon": round(sum(p["lon"] for p in pts) / len(pts), 6),
            "share_within_5min_of_t": round(100 * sum(d <= 400 for d in dists) / len(dists)) if dists else "",
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "chain_lines.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    for filename, data in (("chain_stars.csv", stars), ("chain_brands.csv", summary)):
        with open(os.path.join(OUT_DIR, filename), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            w.writeheader()
            w.writerows(data)

    print("\n----- Constellations -----")
    for s in summary[:12]:
        near_t = f" · {s['share_within_5min_of_t']}% within 5 min of the T" if s["share_within_5min_of_t"] != "" else ""
        print(f"{s['brand']:24} {s['locations']:3} locations · constellation {s['constellation_km']:5} km{near_t}")
    print(f"\nSaved chain_lines.geojson ({len(features)} lines), chain_stars.csv, chain_brands.csv in {OUT_DIR}/")

if __name__ == "__main__":
    main()