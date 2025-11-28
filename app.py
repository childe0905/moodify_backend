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

if not mongo_uri:
    print("❌ 錯誤：找不到 MONGO_URI 環境變數！")

try:
    # 使用 certifi 憑證解決 SSL 問題
    client = MongoClient(mongo_uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
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

# [修正 1] 改回官方正確 API 網址 (原本是 googleusercontent...)
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

spotify_token_cache = {"access_token": None, "expires_at": 0}

def get_spotify_token():
    if time.time() < spotify_token_cache["expires_at"]:
        return spotify_token_cache["access_token"]

    # [修正 2] 改回官方正確 Token 網址
    url = "https://accounts.spotify.com/api/token"
    
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

    # ========================================================
    # [修正 3] 強化搜尋邏輯：分開處理風格與關鍵字
    # ========================================================
    if custom_text:
        # 情況 A: 使用者有手動輸入文字
        final_query = custom_text
        # 如果也有選風格，加在後面輔助
        if genre_ui != "All" and genre_ui in GENRE_MAPPING:
            final_query += f" genre:{GENRE_MAPPING[genre_ui]}"
        random_offset = 0 
    
    elif genre_ui != "All" and genre_ui in GENRE_MAPPING:
        # 情況 B: 使用者選了特定風格 (例如 K-Pop)
        # 策略：不加 "sad/happy" 關鍵字，因為那樣會讓 K-Pop 搜不到結果
        # 改為：只搜風格 + 近年 (避免太舊的歌)
        genre_tag = GENRE_MAPPING[genre_ui]
        final_query = f"genre:{genre_tag} year:2020-2025"
        
        # 風格搜尋結果較少，Offset 設小一點以免搜空
        random_offset = random.randint(0, 10) 
    
    else:
        # 情況 C: 風格選 All，依賴情緒關鍵字
        base_query = get_query_from_metrics(target_valence, target_energy)
        final_query = base_query
        random_offset = random.randint(0, 50) 
    
    # 印出 Log 方便除錯
    print(f"🔍 Searching: '{final_query}' (Offset: {random_offset})")

    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": final_query, "type": "track", "limit": 20, "market": "TW", "offset": random_offset}
    
    try:
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        # Retry 機制 1: 如果隨機頁數沒結果，回到第 0 頁
        if not tracks and random_offset > 0:
            print("⚠️ Offset result empty, retrying offset 0...")
            params["offset"] = 0
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
            tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        # Retry 機制 2: 還是沒結果? 可能是關鍵字太怪，退回純風格搜尋
        if not tracks and genre_ui != "All" and genre_ui in GENRE_MAPPING:
             print("⚠️ Still empty, falling back to pure genre search...")
             fallback_query = f"genre:{GENRE_MAPPING[genre_ui]}"
             params["q"] = fallback_query
             params["offset"] = random.randint(0, 20)
             res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
             tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        # Retry 機制 3: 真的完全沒結果，搜流行歌 (保底)
        if not tracks:
            print("⚠️ Fallback to generic Pop...")
            params["q"] = "Pop"
            params["offset"] = 0
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
            tracks = res.json().get("tracks", {}).get("items", []) if res.status_code == 200 else []

        if not tracks: return jsonify({"error": "No tracks found"}), 404

        # ========================================================
        # 挑選最佳匹配的歌曲 (Audio Features Analysis)
        # ========================================================
        best_match = None
        
        # 取得這一批歌的特徵 (一次最多抓 50 首，這裡 params limit 是 20)
        track_ids = ",".join([t["id"] for t in tracks])
        feat_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features", headers=headers, params={"ids": track_ids})
        feats = [f for f in feat_res.json().get("audio_features", []) if f]
        feat_map = {f["id"]: f for f in feats}

        weighted_tracks = []
        for t in tracks:
            f = feat_map.get(t["id"])
            if not f: continue
            
            # 計算距離：這首歌的情緒 vs 使用者設定的情緒
            dist = math.sqrt((f["valence"] - target_valence)**2 + (f["energy"] - target_energy)**2)
            t["features"] = f
            t["distance"] = dist
            weighted_tracks.append(t)
        
        if weighted_tracks:
            # 根據距離排序，越接近 0 代表越符合
            weighted_tracks.sort(key=lambda x: x["distance"])
            # 從最符合的前 5 首裡面隨機挑一首 (增加驚喜感)
            top_candidates = weighted_tracks[:5]
            best_match = random.choice(top_candidates)
        else:
            # 萬一沒抓到特徵，就隨便挑一首
            best_match = random.choice(tracks)

        # 寫入資料庫
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
                # 清理舊資料... (略)
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
    port = int(os.environ.get("PORT", 5050))
    print(f"🚀 Starting Moodify Backend on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False)