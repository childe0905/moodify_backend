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

# 1. 載入 .env 環境變數 (本地開發用，Render 上會直接讀取設定好的變數)
load_dotenv()

app = Flask(__name__)

# ==========================================
# 強力開啟 CORS (允許所有來源連線)
# ==========================================
CORS(app, resources={r"/*": {"origins": "*"}})

app.config["JSON_AS_ASCII"] = False

# =====================================================
#  資料庫設定 (MongoDB Configuration)
# =====================================================
mongo_uri = os.getenv("MONGO_URI")

# 簡單的防呆：如果沒設定 URI (例如忘記在 Render 設定)，印出錯誤
if not mongo_uri:
    print("❌ 錯誤：找不到 MONGO_URI 環境變數！")

try:
    # 使用 certifi 憑證解決 SSL 問題
    client = MongoClient(mongo_uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
    # 測試連線
    # client.admin.command('ping') # Render 部署時可註解掉這行以加速啟動
    print(f"✅ MongoDB 連線設定完成")
except Exception as e:
    print(f"❌ MongoDB 連線失敗: {e}")

db = client['moodify_db']
users_col = db['users']
records_col = db['mood_records']

# =====================================================
#  Spotify Helper
# =====================================================
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

spotify_token_cache = {"access_token": None, "expires_at": 0}

def get_spotify_token():
    if time.time() < spotify_token_cache["expires_at"]:
        return spotify_token_cache["access_token"]

    TOKEN_URL = "https://accounts.spotify.com/api/token"
    payload = {"grant_type": "client_credentials"}
    try:
        resp = requests.post(url, data=payload, auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))
        if resp.status_code != 200: 
            print(f"Spotify Token Error: {resp.text}")
            return None
        data = resp.json()
        spotify_token_cache["access_token"] = data["access_token"]
        spotify_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return data["access_token"]
    except Exception as e: 
        print(f"Token Fetch Exception: {e}")
        return None

def get_query_from_metrics(valence, arousal):
    if valence < 0.4: return "sad" if arousal < 0.4 else "angry"
    elif valence > 0.6: return "chill" if arousal < 0.4 else "party"
    return "pop"

GENRE_MAPPING = {
    "Mandopop": "mandopop", "K-Pop": "k-pop", "J-Pop": "j-pop",
    "Jazz": "jazz", "Lofi": "lo-fi", "R&B": "r-n-b",
    "Classical": "classical", "Electronic": "electronic"
}

# =====================================================
#  API Routes
# =====================================================
@app.route('/', methods=['GET'])
def index():
    return "Moodify Backend is Running on Render! 🚀"

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    if not username: return jsonify({"error": "No username"}), 400
    
    try:
        user = users_col.find_one({"username": username})
        if user:
            user_id = str(user["_id"])
        else:
            result = users_col.insert_one({
                "username": username,
                "created_at": datetime.now()
            })
            user_id = str(result.inserted_id)
        
        return jsonify({"message": "OK", "user_id": user_id, "username": username})
    except Exception as e:
        print(f"Login Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/history/<user_id>', methods=['GET'])
def get_history(user_id):
    try:
        # 只抓最新的 40 筆
        cursor = records_col.find({"user_id": user_id}).sort("timestamp", -1).limit(40)
        
        return jsonify([{
            "id": str(r["_id"]),
            "date": r["timestamp"].strftime("%m/%d %H:%M"),
            "mood": r.get("mood_tag"),
            "valence": r.get("valence"),
            "energy": r.get("energy"),
            "song": r.get("song_name"),
            "artist": r.get("artist"),
            "image": r.get("image_url"),
            "spotify_url": r.get("spotify_url")
        } for r in cursor])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/spotify/recommend", methods=["POST"])
def spotify_recommend():
    token = get_spotify_token()
    if not token: return jsonify({"error": "Token error"}), 500

    data = request.get_json()
    user_id = data.get("user_id")
    target_valence = float(data.get("valence", 0.5))
    target_energy = float(data.get("arousal", 0.5))
    genre_ui = data.get("genre", "All")
    custom_text = data.get("text", "").strip()

    # 1. 決定搜尋關鍵字
    if custom_text:
        base_query = custom_text
        random_offset = 0 
    else:
        base_query = get_query_from_metrics(target_valence, target_energy)
        random_offset = random.randint(0, 50) 
    
    if genre_ui != "All" and genre_ui in GENRE_MAPPING:
        genre_tag = GENRE_MAPPING[genre_ui]
        final_query = f"{base_query} genre:{genre_tag}"
    else:
        final_query = base_query

    headers = {"Authorization": f"Bearer {token}"}
    
    print(f"🔍 Searching: '{final_query}' (Offset: {random_offset})")
    params = {"q": final_query, "type": "track", "limit": 20, "market": "TW", "offset": random_offset}
    
    try:
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        if not tracks and random_offset > 0:
            params["offset"] = 0
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
            tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        if not tracks and genre_ui != "All" and genre_ui in GENRE_MAPPING:
            fallback_query = f"genre:{GENRE_MAPPING[genre_ui]}"
            params["q"] = fallback_query
            params["offset"] = random.randint(0, 50)
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
            tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        if not tracks:
            params["q"] = "Pop"
            params["offset"] = 0
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
            tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        if not tracks: return jsonify({"error": "No tracks found"}), 404

        best_match = None
        if custom_text:
            candidates = tracks[:5]
            best_match = random.choice(candidates)
            try:
                feat_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features", headers=headers, params={"ids": best_match["id"]})
                feats = feat_res.json().get("audio_features", [])
                best_match["features"] = feats[0] if feats and feats[0] else {"valence": 0.5, "energy": 0.5}
            except:
                best_match["features"] = {"valence": 0.5, "energy": 0.5}
        else:
            track_ids = ",".join([t["id"] for t in tracks[:20]])
            feat_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features", headers=headers, params={"ids": track_ids})
            feats = [f for f in feat_res.json().get("audio_features", []) if f]
            feat_map = {f["id"]: f for f in feats}

            weighted_tracks = []
            for t in tracks:
                f = feat_map.get(t["id"])
                if not f: continue
                dist = math.sqrt((f["valence"] - target_valence)**2 + (f["energy"] - target_energy)**2)
                t["features"] = f
                t["distance"] = dist
                weighted_tracks.append(t)
            
            if weighted_tracks:
                weighted_tracks.sort(key=lambda x: x["distance"])
                top_candidates = weighted_tracks[:5]
                best_match = random.choice(top_candidates)
            else:
                best_match = random.choice(tracks[:5])

        if user_id:
            try:
                records_col.insert_one({
                    "user_id": user_id,
                    "user_input": custom_text if custom_text else "Slider Mode",
                    "mood_tag": genre_ui if genre_ui != "All" else "General",
                    "valence": target_valence,
                    "energy": target_energy,
                    "song_name": best_match["name"],
                    "artist": best_match["artists"][0]["name"],
                    "image_url": best_match["album"]["images"][0]["url"],
                    "spotify_url": best_match["external_urls"]["spotify"],
                    "timestamp": datetime.now()
                })

                LIMIT = 40
                count = records_col.count_documents({"user_id": user_id})
                if count > LIMIT:
                    num_to_delete = count - LIMIT
                    cursor_to_delete = records_col.find({"user_id": user_id},{"_id": 1}).sort("timestamp", 1).limit(num_to_delete)
                    ids_to_delete = [doc["_id"] for doc in cursor_to_delete]
                    if ids_to_delete:
                        records_col.delete_many({"_id": {"$in": ids_to_delete}})

            except Exception as db_e:
                print(f"Database Error: {db_e}")

        return jsonify({
            "name": best_match["name"],
            "artists": ", ".join(a["name"] for a in best_match["artists"]),
            "spotify_url": best_match["external_urls"]["spotify"],
            "album_image": best_match["album"]["images"][0]["url"],
            "preview_url": best_match["preview_url"],
            "match_info": best_match.get("features", {})
        })
    except Exception as e:
        print(f"General Error: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

if __name__ == "__main__":
    # ========================================================
    # 關鍵修改：從環境變數取得 PORT，並設定 host='0.0.0.0'
    # ========================================================
    port = int(os.environ.get("PORT", 5050))
    print(f"🚀 Starting Moodify Backend on port {port}...")
    # debug=False 比較適合正式環境
    app.run(host="0.0.0.0", port=port, debug=False)