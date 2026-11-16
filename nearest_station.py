"""Add the distance from every restaurant to its nearest T station (free, no ArcGIS credits).

Reads  data/citywide_public.csv          (made by build_citywide.py)
Writes data/citywide_public_t.csv         (same rows + nearest_t_station, nearest_t_line, dist_to_t_m, within_5min_walk)
Prints a chains-vs-independents comparison for section 3.
"""
import csv
import math
import statistics
import requests

STATIONS_URL = ("https://gis.massdot.state.ma.us/arcgis/rest/services/"
                "Multimodal/RapidTransitStations/MapServer/0/query")
IN_FILE = "data/citywide_public.csv"
OUT_FILE = "data/citywide_public_t.csv"
FIVE_MIN_WALK_M = 400          # roughly a 5-minute walk
HEADERS = {"User-Agent": "boston-food-geography/0.1 (your-email@example.com)"}

def meters(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def fetch_stations():
    """MBTA rapid transit stations from MassDOT's public map service, as latitude/longitude."""
    r = requests.get(STATIONS_URL, params={"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson"},
                     headers=HEADERS, timeout=60)
    r.raise_for_status()
    stations = []
    for f in r.json()["features"]:
        lon, lat = f["geometry"]["coordinates"][:2]
        p = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
        stations.append({"name": p.get("station") or p.get("name") or "?", "line": p.get("line") or "",
                         "lat": lat, "lon": lon})
    return stations

def main():
    stations = fetch_stations()
    print(f"{len(stations)} T station points downloaded")

    with open(IN_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames + ["nearest_t_station", "nearest_t_line", "dist_to_t_m", "within_5min_walk"]
        rows = list(reader)

    for row in rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        best = min(stations, key=lambda s: meters(lat, lon, s["lat"], s["lon"]))
        d = meters(lat, lon, best["lat"], best["lon"])
        row["nearest_t_station"], row["nearest_t_line"] = best["name"], best["line"]
        row["dist_to_t_m"] = round(d)
        row["within_5min_walk"] = int(d <= FIVE_MIN_WALK_M)

    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)

    # Section 3: chains vs independents
    print("\n----- Chains vs. independents (straight-line distance to the nearest T station) -----")
    for label, flag in (("Chains", "1"), ("Independents", "0")):
        group = [r for r in rows if r["is_chain"] == flag]
        if not group:
            continue
        dists = [r["dist_to_t_m"] for r in group]
        share = 100 * sum(r["within_5min_walk"] for r in group) / len(group)
        print(f"{label:13} {len(group):5} restaurants · median {statistics.median(dists):5.0f} m · "
              f"average {statistics.mean(dists):5.0f} m · {share:4.0f}% within a 5-minute walk")
    print(f"\nSaved {OUT_FILE}")

if __name__ == "__main__":
    main()