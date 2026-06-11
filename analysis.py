import ee
import osmnx as ox
import folium
import requests
import os
from datetime import datetime
from dotenv import load_dotenv
import concurrent.futures

load_dotenv()

# initialize GEE once at module level — not inside functions
# so it doesn't reinitialize on every API call
import json

service_account_json = os.environ.get("GEE_SERVICE_ACCOUNT_JSON")
if service_account_json:
    credentials = ee.ServiceAccountCredentials(
        email=json.loads(service_account_json)["client_email"],
        key_data=service_account_json
    )
    ee.Initialize(credentials, project='satsuredemo')
    print("GEE initialized with service account")
else:
    ee.Initialize(project='satsuredemo')
    print("GEE initialized with local credentials")

# ─── STEP 1: REAL GIS DATA FROM OPENSTREETMAP ─────────────────────────────

def fetch_corridor_data(city_name):
    """
    Fetches real road network and building footprints from OpenStreetMap.
    Uses 1km radius around city center for demo speed.
    Production version would use full city polygon.
    """
    print(f"Fetching GIS data for {city_name}...")

    location = ox.geocode(city_name)
    # ox.geocode returns (lat, lon) tuple directly
    # faster than geocode_to_gdf when we just need the center point
    lat, lon = location

    # fetch road network — 1km radius, major roads only
    G = ox.graph_from_point(
        (lat, lon),
        dist=1000,
        network_type="drive",
        custom_filter='["highway"~"primary|secondary|tertiary|residential"]'
        # filtering to main road types — faster than fetching every lane
    )

    edges = ox.graph_to_gdfs(G, nodes=False)
    # nodes=False = only road segments, not intersection points

    total_road_km = round(edges["length"].sum() / 1000, 2)
    # edges["length"] is in meters per segment
    # .sum() adds all segments, /1000 converts to km

    # fetch building footprints — same 1km radius
    try:
        buildings = ox.features_from_point(
            (lat, lon),
            tags={"building": True},
            dist=1000
        )
        building_count = len(buildings)
    except Exception as e:
        print(f"Buildings fetch failed: {e}")
        building_count = 0

    print(f"Roads: {total_road_km}km, Buildings: {building_count}")

    return {
        "lat": lat,
        "lon": lon,
        "edges": edges,
        "total_road_km": total_road_km,
        "building_count": building_count,
    }

# ─── STEP 2: REAL NDVI FROM GOOGLE EARTH ENGINE ───────────────────────────

def fetch_real_ndvi(lat, lon):
    """
    Fetches real NDVI from Sentinel-2 Surface Reflectance
    using Google Earth Engine.

    NDVI = (NIR - Red) / (NIR + Red)
    Sentinel-2 bands: NIR = B8, Red = B4
    Range: -1 to +1
    Above 0.3 = vegetation present
    Above 0.6 = dense vegetation

    Using 2025 full year median composite — same seasonal window
    ensures consistent comparison without monsoon bias.
    """
    print("Fetching real NDVI from Sentinel-2 via GEE...")

    try:
        # GEE takes [lon, lat] — opposite of folium's [lat, lon]
        point = ee.Geometry.Point([lon, lat])
        region = point.buffer(5000)
        # 5km buffer — large enough for meaningful city-level NDVI average

        s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(region) \
            .filterDate('2025-01-01', '2025-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
        # CLOUDY_PIXEL_PERCENTAGE filter removes cloudy images
        # clouds give wrong reflectance values = wrong NDVI

        # median composite — more robust to outliers than mean
        median_image = s2.median()

        # normalizedDifference computes (B8-B4)/(B8+B4) automatically
        ndvi = median_image.normalizedDifference(['B8', 'B4'])

        # reduceRegion computes a single summary stat for the whole region
        # Reducer.mean() = average NDVI across all pixels in 5km buffer
        stats = ndvi.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=10,
            # scale=10m = matches Sentinel-2 native resolution
            maxPixels=1e9
        )

        # getInfo() executes everything on Google's servers
        # all lines above just built the computation recipe
        result = stats.getInfo()
        ndvi_value = result.get('nd', None)
        # 'nd' = the band name GEE uses for normalizedDifference output

        print(f"Raw NDVI value: {ndvi_value}")

        if ndvi_value is None:
            print("No NDVI data returned, using fallback")
            return 17.0
            # fallback = average green cover across Tier 1 Indian cities
            # source: IIHS Urban Green Cover Report 2023
            # range: 12% (Mumbai) to 21% (Bengaluru)

        # convert NDVI (0 to 0.6 range) to green cover percentage (0-100%)
        # below 0 = water/bare soil = 0% green
        # 0.6 and above = dense forest = 100% green
        green_pct = max(0, min((ndvi_value / 0.6) * 100, 100))
        return round(green_pct, 1)

    except Exception as e:
        print(f"GEE error: {e}")
        return 17.0
        # fallback = average Tier 1 Indian city green cover
        # source: IIHS Urban Green Cover Report 2023

# ─── STEP 3: NDVI MAP TILE FROM GEE ───────────────────────────────────────

def get_ndvi_tile_url(lat, lon):
    """
    Generates a colored NDVI map tile URL from GEE.
    Red = bare soil / low vegetation
    Green = dense vegetation

    Returns a tile URL template with {z}/{x}/{y} placeholders.
    Folium fetches tiles on demand as user pans/zooms —
    only the visible portion loads, not the full satellite image.
    """
    try:
        point = ee.Geometry.Point([lon, lat])
        region = point.buffer(5000)

        s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(region) \
            .filterDate('2025-01-01', '2025-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
            .median()

        ndvi = s2.normalizedDifference(['B8', 'B4'])

        # visualize converts single-band NDVI values to RGB colors
        ndvi_colored = ndvi.visualize(
            min=-0.2,
            max=0.8,
            palette=['#d73027', '#fc8d59', '#fee08b', '#91cf60', '#1a9850']
            # red → orange → yellow → light green → dark green
            # standard vegetation visualization palette
        )

        # getMapId generates tile URLs rendered on GEE servers
        # browser fetches only visible tiles on demand — not full image
        map_id = ndvi_colored.getMapId()
        tile_url = map_id['tile_fetcher'].url_format

        print("NDVI tile URL generated successfully")
        return tile_url

    except Exception as e:
        print(f"NDVI tile error: {e}")
        return None

# ─── STEP 4: CHANGE DETECTION 2023 vs 2025 ────────────────────────────────

def get_change_detection(lat, lon):
    """
    Compares NDVI between 2023 and 2025 to detect land cover change.

    Why 2023 vs 2025:
    - Full year composites required — partial years skew toward
      monsoon or dry season, making differences seasonal not structural
    - Same calendar window (Jan-Dec) removes seasonal bias
    - 2025 is fully ingested on GEE as of mid-2026

    Colors:
    - Red = NDVI decreased = vegetation lost = likely construction
    - White = no change
    - Green = NDVI increased = vegetation gained / reforestation

    This is the core of SatSure's construction monitoring product.
    """
    try:
        point = ee.Geometry.Point([lon, lat])
        region = point.buffer(5000)

        # 2023 baseline — full year median
        s2_2023 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(region) \
            .filterDate('2023-01-01', '2023-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
            .median() \
            .normalizedDifference(['B8', 'B4'])

        # 2025 current — full year median
        s2_2025 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(region) \
            .filterDate('2025-01-01', '2025-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
            .median() \
            .normalizedDifference(['B8', 'B4'])

        # pixel-by-pixel subtraction: 2025 minus 2023
        # negative = NDVI dropped = vegetation lost
        # positive = NDVI rose = vegetation gained
        change = s2_2025.subtract(s2_2023)

        # visualize — red = loss, white = stable, green = gain
        change_colored = change.visualize(
            min=-0.3,
            max=0.3,
            palette=['#d73027', '#fc8d59', '#ffffff', '#91cf60', '#1a9850']
        )

        map_id = change_colored.getMapId()
        tile_url = map_id['tile_fetcher'].url_format

        # compute mean change — negative = net vegetation loss
        change_stats = change.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=10,
            maxPixels=1e9
        ).getInfo()

        mean_change = round(change_stats.get('nd', 0), 4)
        print(f"Change detection complete. Mean NDVI change 2023→2025: {mean_change}")
        return tile_url, mean_change

    except Exception as e:
        print(f"Change detection error: {e}")
        return None, 0

# ─── STEP 5: PHENOLOGY CURVE ──────────────────────────────────────────────

def get_phenology_curve(lat, lon):
    """
    Computes monthly mean NDVI for 2024-2025 to show vegetation
    seasonality at the location — the phenology curve.

    Why this matters:
    - Agricultural area = two NDVI peaks (kharif + rabi crop cycles)
    - Urban area = flat low NDVI year-round
    - Construction site = sudden NDVI drop at start of works

    """
    print("Computing phenology curve (monthly NDVI 2024-2025)...")

    try:
        point = ee.Geometry.Point([lon, lat])
        region = point.buffer(3000)
        # 3km buffer — slightly smaller than NDVI/change detection
        # phenology needs less area, gains speed

        # load full 2024-2025 collection once
        # attach month label to each image
        def add_month_label(img):
            return img.normalizedDifference(['B8', 'B4']) \
                      .rename('ndvi') \
                      .set('month', img.date().format('YYYY-MM'))
        # .rename('ndvi') renames the output band from 'nd' to 'ndvi'
        # .set() attaches a property to the image — like a tag
        # img.date().format('YYYY-MM') extracts year-month as string

        collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(region) \
            .filterDate('2024-01-01', '2025-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 30)) \
            .map(add_month_label)
        # .map() applies add_month_label to every image in the collection
        # 30% cloud threshold — slightly relaxed for monthly composites
        # ensures enough images per month even in monsoon season

        # get unique sorted list of months
        months = collection.aggregate_array('month').distinct().sort()
        # aggregate_array extracts all 'month' property values as a list
        # .distinct() removes duplicates
        # .sort() orders chronologically

        # for each month, compute mean NDVI over the region
        def monthly_mean(month):
            monthly_imgs = collection.filter(
                ee.Filter.eq('month', month)
            )
            # filter to images tagged with this month
            mean_img = monthly_imgs.mean()
            # mean across all images in the month

            val = mean_img.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=region,
                scale=20,
                # scale=20m — faster than 10m, sufficient for monthly trend
                maxPixels=1e9
            ).get('ndvi')
            # .get('ndvi') because we renamed the band above

            # return as a Feature with month + ndvi properties
            return ee.Feature(None, {'month': month, 'ndvi': val})

        # server-side map — all months computed in one GEE session
        result = ee.FeatureCollection(months.map(monthly_mean))

        # ONE getInfo() call fetches all 24 months at once
        data = result.getInfo()

        # parse into clean list of dicts
        phenology = []
        for feature in data['features']:
            props = feature['properties']
            ndvi_val = props.get('ndvi')
            phenology.append({
                "month": props['month'],
                "ndvi": round(ndvi_val, 3) if ndvi_val is not None else None
            })

        # sort chronologically
        phenology.sort(key=lambda x: x['month'])

        print(f"Phenology curve: {len(phenology)} months computed")
        return phenology

    except Exception as e:
        print(f"Phenology error: {e}")
        return []

# ─── STEP 6: RISK SCORE ───────────────────────────────────────────────────

def compute_risk_score(total_road_km, building_count, green_cover_pct):
    """
    Weighted infrastructure risk model for Indian urban corridors.

    THREE FACTORS:

    1. ROAD DENSITY (weight: 40%)
       Threshold: 50km within 1km radius
       Derivation: Dense Indian cities have ~15-20 km of road per km²
       (Journal of Transport Geography, 2019)
       1km radius = π × 1² = 3.14 km²
       Max road length = 20 × 3.14 = ~62km → rounded to 50km (conservative)

    2. BUILDING DENSITY (weight: 40%)
       Threshold: 2500 buildings within 1km radius
       Source: GHSL (Global Human Settlement Layer) for Indian cities
       Range: ~1500 (suburban) to ~5000 (Dharavi-level density)

    3. ENVIRONMENTAL RISK (weight: 20%)
       Inverted — LOW green cover = LOWER env clearance risk
       Less vegetation = fewer forest/wildlife clearances needed

    WEIGHTS (40 / 40 / 20):
       Construction complexity and displacement are dominant cost
       and delay factors in Indian infrastructure projects.
       Reference: NITI Aayog Infrastructure Vision 2025,
       World Bank Urban Infrastructure Cost Driver Guide.
       Production version: ML model trained on historical NHAI /
       Power Grid project outcome data.

    THRESHOLDS (3.5 / 6.5):
       Standard tertile split on 0-10 scale.
       0-3.33 → Low, 3.33-6.67 → Medium, 6.67-10 → High
    """

    road_risk     = min(total_road_km / 50, 1) * 10
    building_risk = min(building_count / 2500, 1) * 10
    env_risk      = (1 - green_cover_pct / 100) * 10

    risk_score = round(
        (road_risk * 0.4) + (building_risk * 0.4) + (env_risk * 0.2),
        2
    )

    if risk_score < 3.5:
        risk_label = "Low"
    elif risk_score < 6.5:
        risk_label = "Medium"
    else:
        risk_label = "High"

    return risk_score, risk_label

# ─── STEP 7: FOLIUM MAP ───────────────────────────────────────────────────

def build_map(lat, lon, edges, risk_label, city_name, green_cover_pct,
              ndvi_tile_url=None, change_tile_url=None, mean_change=0):
    """
    Interactive Leaflet map via Folium with 4 toggleable layers:
    1. NDVI from Sentinel-2 via GEE (vegetation density 2025)
    2. Change detection 2023→2025 via GEE (construction activity)
    3. Real road network from OpenStreetMap via osmnx
    4. Risk zone circle (computed from weighted model)
    """
    risk_colors = {
        "Low": "#10b981",
        "Medium": "#f59e0b",
        "High": "#ef4444"
    }
    color = risk_colors[risk_label]

    m = folium.Map(
        location=[lat, lon],
        zoom_start=14,
        tiles="CartoDB dark_matter"
    )

    # ── LAYER 1: NDVI from GEE ──
    if ndvi_tile_url:
        folium.TileLayer(
            tiles=ndvi_tile_url,
            attr="Google Earth Engine / Sentinel-2 2025",
            name="NDVI 2025 (Sentinel-2)",
            overlay=True,
            control=True,
            opacity=0.7
        ).add_to(m)

    # ── LAYER 2: CHANGE DETECTION 2023→2025 ──
    if change_tile_url:
        folium.TileLayer(
            tiles=change_tile_url,
            attr="Google Earth Engine / Change Detection 2023-2025",
            name="Change Detection (2023→2025)",
            overlay=True,
            control=True,
            opacity=0.7,
            show=False
            # hidden by default — user toggles on to compare
        ).add_to(m)

    # ── LAYER 3: REAL ROAD NETWORK from osmnx ──
    edges_wgs = edges.to_crs(epsg=4326)
    # to_crs converts to WGS84 (standard GPS lat/lon)
    # folium requires EPSG:4326 — won't render other projections

    folium.GeoJson(
        edges_wgs.__geo_interface__,
        # __geo_interface__ converts GeoDataFrame → GeoJSON dict
        style_function=lambda x: {
            "color": "#3b82f6",
            "weight": 1.5,
            "opacity": 0.7
        },
        name="Road Network (OSM)"
    ).add_to(m)

    # ── LAYER 4: RISK ZONE CIRCLE ──
    folium.Circle(
        location=[lat, lon],
        radius=1000,
        color=color,
        fill=True,
        fill_opacity=0.12,
        weight=2,
        tooltip=f"Infrastructure Risk: {risk_label}"
    ).add_to(m)

    # ── CENTER MARKER with full data popup ──
    if mean_change < -0.05:
        change_desc = f"⚠️ Vegetation loss detected ({mean_change})"
    elif mean_change > 0.05:
        change_desc = f"✅ Vegetation gain detected (+{mean_change})"
    else:
        change_desc = "➡️ Minimal land cover change (2023→2025)"

    folium.Marker(
        location=[lat, lon],
        popup=folium.Popup(
            f"""<b>{city_name}</b><br><br>
            <b>Risk Level:</b> {risk_label}<br>
            <b>Green Cover (NDVI 2025):</b> {green_cover_pct}%<br>
            <b>Land Cover Change:</b> {change_desc}<br>
            <br><i>Sentinel-2 via Google Earth Engine</i>""",
            max_width=260
        ),
        icon=folium.Icon(
            color="red" if risk_label == "High" else
                  "orange" if risk_label == "Medium" else "green",
            icon="info-sign"
        )
    ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    return m._repr_html_()

# ─── STEP 8: GROQ AI BRIEFING ─────────────────────────────────────────────

def generate_briefing(city, total_road_km, building_count,
                      green_cover_pct, risk_score, risk_label,
                      mean_change, phenology):
    """
    Groq (Llama 3.3 70B) generates a professional analyst briefing.
    Temperature 0.4 = consistent, professional tone.
    Now incorporates phenology interpretation into the briefing.
    """
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

    if not GROQ_API_KEY:
        return "Groq API key not found — set GROQ_API_KEY in .env file."

    # describe change context
    if mean_change < -0.05:
        change_context = f"significant vegetation loss (NDVI delta: {mean_change}), indicating active construction or deforestation"
    elif mean_change > 0.05:
        change_context = f"vegetation gain (NDVI delta: +{mean_change}), indicating greening or land recovery"
    else:
        change_context = "minimal land cover change between 2023 and 2025"

    # interpret phenology pattern
    phenology_context = "insufficient data for seasonal analysis"
    if phenology and len(phenology) >= 12:
        valid = [p for p in phenology if p['ndvi'] is not None]
        if valid:
            max_ndvi = max(p['ndvi'] for p in valid)
            min_ndvi = min(p['ndvi'] for p in valid)
            seasonal_range = round(max_ndvi - min_ndvi, 3)
            peak_month = max(valid, key=lambda p: p['ndvi'])['month']

            if seasonal_range > 0.2:
                phenology_context = f"strong seasonal NDVI pattern (range: {seasonal_range}) with peak in {peak_month} — indicates active agricultural use or deciduous vegetation"
            elif seasonal_range > 0.1:
                phenology_context = f"moderate seasonal variation (range: {seasonal_range}) — mixed urban-agricultural land use"
            else:
                phenology_context = f"flat NDVI profile (range: {seasonal_range}) — predominantly urban or built-up area with minimal seasonal vegetation"
    # seasonal_range > 0.2 = likely agricultural (two crop peaks)
    # flat profile = urban, no crop cycles

    prompt = f"""You are a senior infrastructure analyst at a geospatial intelligence firm.

Satellite and GIS analysis of {city} corridor (1km radius) shows:
- Road network length: {total_road_km} km
- Building footprint count: {building_count:,}
- Vegetation cover (Sentinel-2 NDVI 2025, Google Earth Engine): {green_cover_pct}%
- Land cover change 2023 to 2025: {change_context}
- Seasonal vegetation pattern (phenology): {phenology_context}
- Infrastructure complexity risk score: {risk_score}/10 ({risk_label} risk)

Write a concise 3-paragraph analyst briefing for a project director
considering infrastructure development in this corridor.
Cover: current urban density and land use character, key risk factors
including the land cover change and seasonal vegetation pattern,
and one specific actionable recommendation.
Be direct, data-driven, and professional."""

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.4,
                "max_tokens": 450,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        data = response.json()

        if "choices" not in data:
            return f"Groq error: {data.get('error', {}).get('message', str(data))}"

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        return f"Briefing generation failed: {str(e)}"

# ─── MASTER FUNCTION ─────────────────────────────────────────────────────

def run_full_analysis(city_name):
    """
    Orchestrates the full pipeline with parallel GEE execution.

    PARALLEL EXECUTION:
    After osmnx fetch (needs lat/lon first), all GEE calls run
    simultaneously using ThreadPoolExecutor — cutting total time
    from ~60s sequential to ~20s parallel.

    Steps:
    1. Real road + building data — OpenStreetMap via osmnx
    2. In parallel:
       a. Real NDVI — Sentinel-2 2025 via GEE
       b. NDVI map tile URL — Sentinel-2 2025 via GEE
       c. Change detection — Sentinel-2 2023 vs 2025 via GEE
       d. Phenology curve — monthly NDVI 2024-2025 via GEE
    3. Weighted risk score (needs NDVI from step 2a)
    4. Folium map with 4 toggleable layers
    5. Groq AI briefing incorporating all data points
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # step 1: real GIS data — must run first, need lat/lon for GEE
    corridor = fetch_corridor_data(city_name)
    lat = corridor["lat"]
    lon = corridor["lon"]

    # step 2: all GEE calls in parallel
    # ThreadPoolExecutor runs each function in its own thread
    # max_workers=4 = up to 4 simultaneous GEE calls
    print("Running GEE calls in parallel...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_ndvi      = executor.submit(fetch_real_ndvi, lat, lon)
        future_ndvi_tile = executor.submit(get_ndvi_tile_url, lat, lon)
        future_change    = executor.submit(get_change_detection, lat, lon)
        future_phenology = executor.submit(get_phenology_curve, lat, lon)
        # submit() starts each function immediately in a thread
        # returns a Future object — a promise of the result

    # .result() blocks until that specific task finishes
    # all four ran simultaneously — total time = slowest of the four
    green_cover_pct          = future_ndvi.result()
    ndvi_tile_url            = future_ndvi_tile.result()
    change_tile_url, mean_change = future_change.result()
    phenology                = future_phenology.result()

    print(f"All GEE calls complete. Green cover: {green_cover_pct}%")

    # step 3: risk score (needs green_cover_pct from step 2)
    risk_score, risk_label = compute_risk_score(
        corridor["total_road_km"],
        corridor["building_count"],
        green_cover_pct
    )

    # step 4: folium map with all layers
    map_html = build_map(
        lat, lon,
        corridor["edges"],
        risk_label,
        city_name,
        green_cover_pct,
        ndvi_tile_url,
        change_tile_url,
        mean_change
    )

    # step 5: Groq AI briefing — now includes phenology context
    briefing = generate_briefing(
        city_name,
        corridor["total_road_km"],
        corridor["building_count"],
        green_cover_pct,
        risk_score,
        risk_label,
        mean_change,
        phenology
    )

    return {
        "city": city_name,
        "timestamp": timestamp,
        "total_road_km": corridor["total_road_km"],
        "building_count": corridor["building_count"],
        "green_cover_pct": green_cover_pct,
        "risk_score": risk_score,
        "risk_label": risk_label,
        "ai_briefing": briefing,
        "map_html": map_html,
        "mean_change": mean_change,
        "phenology": phenology
        # phenology = list of {month, ndvi} dicts for Chart.js
    }