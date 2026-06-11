from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from analysis import run_full_analysis
from database import init_db, save_analysis, get_all_analyses, get_map_html

load_dotenv()
# loads .env file into environment variables
# must happen before anything reads os.environ

app = Flask(__name__)
# __name__ tells Flask where to find templates/ and static/ folders

# ─── ROUTE 1: HOME PAGE ───────────────────────────────────────────────────

@app.route("/")
def home():
    past_analyses = get_all_analyses()
    # fetch last 10 analyses from SQLite for the history table
    
    return render_template("index.html", analyses=past_analyses)
    # render_template loads templates/index.html
    # analyses=past_analyses passes Python data into Jinja2 template
    # accessible in HTML as {{ analyses }}

# ─── ROUTE 2: RUN ANALYSIS ────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    # methods=["POST"] = only accepts POST requests
    # GET requests to this URL will return 405 Method Not Allowed
    
    city = request.json.get("city")
    # request.json parses the POST body as JSON
    # .get("city") extracts the city field safely — returns None if missing

    if not city:
        return jsonify({"error": "Please enter a city name"}), 400
        # 400 = Bad Request — client sent invalid input

    try:
        result = run_full_analysis(city)
        # runs the full pipeline:
        # osmnx → GEE NDVI → GEE change detection → GEE phenology
        # → risk score → folium map → Groq briefing
        # all GEE calls run in parallel via ThreadPoolExecutor
        
        save_analysis(result)
        # persist to SQLite — stores everything including map_html

        # return metrics + briefing + phenology to frontend
        # map_html is NOT included here — it's large (500KB-1MB)
        # served separately via /map/<id> into an iframe (lazy loading)
        return jsonify({
            "city":            result["city"],
            "timestamp":       result["timestamp"],
            "total_road_km":   result["total_road_km"],
            "building_count":  result["building_count"],
            "green_cover_pct": result["green_cover_pct"],
            "risk_score":      result["risk_score"],
            "risk_label":      result["risk_label"],
            "ai_briefing":     result["ai_briefing"],
            "mean_change":     result["mean_change"],
            "phenology":       result.get("phenology", [])
            # ↑ THIS WAS MISSING — phenology data for Chart.js
            # result.get() with default [] prevents KeyError
            # if phenology failed silently, frontend gets empty array
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        # 500 = Internal Server Error
        # str(e) converts exception object to readable message
        # frontend shows this in an alert()

# ─── ROUTE 3: SERVE MAP HTML ──────────────────────────────────────────────

@app.route("/map/<int:analysis_id>")
def get_map(analysis_id):
    # <int:analysis_id> = URL path parameter
    # /map/3 automatically sets analysis_id = 3 as an integer
    
    map_html = get_map_html(analysis_id)
    # fetches the stored Folium map HTML string from SQLite

    if not map_html:
        return "Map not found", 404
        # 404 = Not Found

    return map_html
    # returns raw HTML string
    # browser renders it directly inside the <iframe> in index.html
    # this is why the map has its own route — lazy loading

# ─── ROUTE 4: HISTORY ─────────────────────────────────────────────────────

@app.route("/history")
def history():
    analyses = get_all_analyses()
    # fetches last 10 analyses from SQLite ordered newest first
    
    return jsonify(analyses)
    # frontend uses this to rebuild the history table
    # and get the latest analysis ID for loading the map

# ─── START ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)