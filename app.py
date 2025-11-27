from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import os
import requests
import time
import math
import random
from dotenv import load_dotenv
from pymongo import MongoClient
from bson.objectid import ObjectId
import certifi 

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config["JSON_AS_ASCII"] = False

# =====================================================
#  Lazy MongoDB (避免 Render 啟動 timeout)
# =====================================================
mongo_uri = os.getenv("MONGO_URI")
client = None
db = None
users_col = None
records_col = None

def get_mongo():
    """第一次用到資料庫再連線"""
    global client, db, users_col, records_col

    if client is None:
        try:
            client = MongoClient(
                mongo_uri,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=5000  # 最長 5 秒
            )
            db = client['moodify_db']
            users_col = db['users']
            records_col = db['mood_records']
            print("✅ MongoDB Connected (lazy mode)")
        except Exception as e:
            print("❌ MongoDB connection failed:", e)

get_mongo  # 不主動連線，等 API 調用時才連

# =====================================================
# Spotify Token（lazy）
# =====================================================
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

SPOTIFY_API_BASE = "https://api.spotify.com/v1"
spotify_cache = {"access": None, "expires": 0}

def get_spotify_token():
    """延後到 request 才抓 token"""
    if time.time() < spotify_cache["expires"]:
        return spotify_cache["access"]

    url = "https://accounts.spotify.com/api/token"
    resp = requests.post(url, data={"grant_type": "client_credentials"},
                         auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))

    if resp.status_code != 200:
        print("Spotify Token Error:", resp.text)
        return None

    data = resp.json()
    spotify_cache["access"] = data["access_token"]
    spotify_cache["expires"] = time.time() + data["expires_in"] - 60
    return data["access_token"]

# =====================================================
# ROUTES
# =====================================================

@app.route("/")
def home():
    return "Moodify Backend Running on Render 🚀"

@app.route("/login", methods=["POST"])
def login():
    get_mongo()
    data = request.json
    username = data.get("username")

    if not username:
        return jsonify({"error": "No username"}), 400

    user = users_col.find_one({"username": username})
    if user:
        return jsonify({"user_id": str(user["_id"]), "username": username})

    result = users_col.insert_one({
        "username": username,
        "created_at": datetime.now()
    })

    return jsonify({"user_id": str(result.inserted_id), "username": username})

@app.route("/spotify/recommend", methods=["POST"])
def recommend_song():
    get_mongo()
    token = get_spotify_token()
    if not token:
        return jsonify({"error": "Spotify Token Failed"}), 500

    data = request.json
    valence = float(data.get("valence", 0.5))
    energy = float(data.get("arousal", 0.5))

    # 簡化版 query
    query = "happy pop" if valence > 0.6 else "sad piano"

    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": query, "type": "track", "limit": 20, "market": "TW"}

    res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
    items = res.json().get("tracks", {}).get("items", [])

    if not items:
        return jsonify({"error": "No songs"}), 404

    song = random.choice(items)

    return jsonify({
        "name": song["name"],
        "artists": ", ".join(a["name"] for a in song["artists"]),
        "spotify_url": song["external_urls"]["spotify"],
        "album_image": song["album"]["images"][0]["url"],
        "preview_url": song["preview_url"],
    })

# =====================================================
# RUN (Render 必須這樣寫)
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"🚀 Running on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False)