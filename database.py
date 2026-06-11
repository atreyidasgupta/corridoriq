import sqlite3 

DATABASE = "corridoriq.db"  # this file gets created automatically when we run it

def init_db():
    """Create the SQLite database file and the 'analyses' table if they don't
    already exist. Safe to call every time the app starts — it won't wipe or
    duplicate existing data"""
    conn = sqlite3.connect(DATABASE)
    
    cursor = conn.cursor()
    
    # CREATE TABLE IF NOT EXISTS — safe to run multiple times, won't duplicate
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            --  unique ID for each analysis, auto-increments
            
            city TEXT NOT NULL,
            --  the city/corridor the user searched
            
            timestamp TEXT NOT NULL,
            -- when the analysis was run
            
            total_road_km REAL,
            -- total road network length in the corridor (km)
            
            building_count INTEGER,
            -- number of buildings detected in the area
            
            green_cover_pct REAL,
            --  percentage vegetation cover 
            
            risk_score REAL,
            -- computed risk score 0-10
            
            risk_label TEXT,
            -- "Low" / "Medium" / "High"
            
            ai_briefing TEXT,
            --  Groq generated analyst summary
            
            map_html TEXT
            -- the entire folium map stored as HTML string
        )
    """)
    
    conn.commit()   # save the changes
    conn.close()    # close the connection — always do this

def save_analysis(data):
    """Insert one completed analysis into the database as a new row.
    Takes a dict whose keys match the table columns (city, timestamp,
    road/building/green metrics, risk score & label, AI briefing, map HTML)
    and persists it. Used after an analysis finishes so it shows up in history."""
    # data is a Python dict with all the fields above
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO analyses (
            city, timestamp, total_road_km, building_count,
            green_cover_pct, risk_score, risk_label, ai_briefing, map_html
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["city"],
        data["timestamp"],
        data["total_road_km"],
        data["building_count"],
        data["green_cover_pct"],
        data["risk_score"],
        data["risk_label"],
        data["ai_briefing"],
        data["map_html"]
    ))
    
    conn.commit()   # save the changes
    conn.close()

def get_all_analyses():
    """Return the recent analyses (newest first) as a list of dicts,
    ready for Flask to JSON-serialize for the dashboard history. The large
    map_html field is deliberately left out here — fetch it on demand with
    get_map_html() to keep this response small."""
    # fetch all past analyses for the dashboard history
    conn = sqlite3.connect(DATABASE)
    
    # row_factory makes each row behave like a dict
    # so you can do row["city"] instead of row[0]
    conn.row_factory = sqlite3.Row
    
    
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, city, timestamp, total_road_km, 
               building_count, green_cover_pct, 
               risk_score, risk_label, ai_briefing
        FROM analyses
        ORDER BY id DESC
        -- ↑ newest first
        LIMIT 10
        -- ↑ only last 10 analyses
    """)
    
    rows = cursor.fetchall()  # get all results as a list
    conn.close()
    
    # convert to list of dicts so Flask can JSON-serialize it
    return [dict(row) for row in rows]

def get_map_html(analysis_id):
    """Return only the stored folium map (as an HTML string) for one analysis,
    looked up by its id. Returns None if no row with that id exists. Kept
    separate from get_all_analyses() because the map HTML is large."""
    # fetch just the map HTML for a specific analysis
    # stored separately because it's large
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT map_html FROM analyses WHERE id = ?",
        (analysis_id,)  # always pass as tuple even for one value
    )
    
    result = cursor.fetchone()  # just one row
    conn.close()
    
    return result["map_html"] if result else None