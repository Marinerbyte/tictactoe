import os
import json
import time
import threading
import io
import random
import websocket
import ssl
import sqlite3
import psycopg2
from flask import Flask, render_template_string, request, jsonify, send_file
from PIL import Image, ImageDraw

app = Flask(__name__)

# =============================================================================
# 1. THREAD LOCKS & GLOBALS (CRITICAL FOR SAFETY)
# =============================================================================
# Ye locks race conditions ko rokenge.
DB_LOCK = threading.Lock()
GAME_LOCK = threading.Lock()

ASSETS = {}

# =============================================================================
# 2. ASSET GENERATION (IN-MEMORY)
# =============================================================================
def init_assets():
    # Board (Dark Cyberpunk)
    board = Image.new('RGB', (900, 900), (15, 15, 20)) 
    draw = ImageDraw.Draw(board)
    grid_color = (0, 243, 255) # Neon Cyan
    
    # Border & Grid
    draw.rectangle([10, 10, 890, 890], outline=grid_color, width=15)
    for xy in [300, 600]:
        draw.line([(xy, 20), (xy, 880)], fill=grid_color, width=15)
        draw.line([(20, xy), (880, xy)], fill=grid_color, width=15)
    ASSETS['board'] = board

    # X (Neon Red)
    x_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_x = ImageDraw.Draw(x_img)
    d_x.line([(60,60), (240,240)], fill=(255, 0, 60), width=25)
    d_x.line([(240,60), (60,240)], fill=(255, 0, 60), width=25)
    ASSETS['x'] = x_img

    # O (Neon Green)
    o_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_o = ImageDraw.Draw(o_img)
    d_o.ellipse([60,60,240,240], outline=(0, 255, 65), width=25)
    ASSETS['o'] = o_img

init_assets()

# =============================================================================
# 3. DATABASE MANAGER (THREAD SAFE)
# =============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_SQLITE = False if DATABASE_URL else True

def get_db():
    if USE_SQLITE: return sqlite3.connect("titan_ttt.db")
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with DB_LOCK: # Lock DB during init
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS tt_players 
                       (username VARCHAR(255) PRIMARY KEY, wins INTEGER, score INTEGER, avatar_url TEXT)''')
            conn.commit()
            conn.close()
        except Exception as e: print("DB Error:", e)

# User tabhi add hoga jab score update hoga (Clean Leaderboard)
def update_score(username, points, avatar_url=""):
    with DB_LOCK: # CRITICAL SECTION: Only one thread can write at a time
        try:
            conn = get_db()
            c = conn.cursor()
            ph = "?" if USE_SQLITE else "%s"
            
            c.execute(f"SELECT score, wins FROM tt_players WHERE username={ph}", (username,))
            data = c.fetchone()
            
            if data:
                new_score = data[0] + points
                new_wins = data[1] + (1 if points > 0 else 0)
                # Update Score and Avatar
                if avatar_url:
                    c.execute(f"UPDATE tt_players SET score={ph}, wins={ph}, avatar_url={ph} WHERE username={ph}", 
                              (new_score, new_wins, avatar_url, username))
                else:
                    c.execute(f"UPDATE tt_players SET score={ph}, wins={ph} WHERE username={ph}", 
                              (new_score, new_wins, username))
            else:
                # First time entry (Only if points != 0 usually, but we allow start)
                # Starting Bonus = 100
                initial_score = 100 + points
                initial_wins = 1 if points > 0 else 0
                c.execute(f"INSERT INTO tt_players (username, score, wins, avatar_url) VALUES ({ph}, {ph}, {ph}, {ph})", 
                          (username, initial_score, initial_wins, avatar_url))
            
            conn.commit()
            conn.close()
        except Exception as e: print(f"Score Update Error: {e}")

def get_score(username):
    # Read-only doesn't strictly need lock, but good for consistency
    try:
        conn = get_db()
        c = conn.cursor()
        ph = "?" if USE_SQLITE else "%s"
        c.execute(f"SELECT score FROM tt_players WHERE username={ph}", (username,))
        data = c.fetchone()
        conn.close()
        return data[0] if data else 0
    except: return 0

def get_leaderboard_data():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT username, score, wins, avatar_url FROM tt_players ORDER BY score DESC LIMIT 50")
        data = c.fetchall()
        conn.close()
        return data
    except: return []

init_db()

# =============================================================================
# 4. BOT CORE & RECONNECT LOGIC
# =============================================================================
BOT = {
    "ws": None, "status": "DISCONNECTED",
    "user": "", "pass": "", "room": "", "domain": "",
    "should_run": False, "avatars": {}
}

CHAT_HISTORY = []
DEBUG_LOGS = []

def save_chat(user, msg, avatar="", type="text"):
    timestamp = time.strftime("%H:%M")
    CHAT_HISTORY.append({"user": user, "msg": msg, "avatar": avatar, "time": timestamp, "type": type})
    if len(CHAT_HISTORY) > 100: CHAT_HISTORY.pop(0)

def save_debug(direction, payload):
    timestamp = time.strftime("%H:%M:%S")
    try:
        if isinstance(payload, str): payload = json.loads(payload)
    except: pass
    DEBUG_LOGS.append({"time": timestamp, "dir": direction, "data": payload})
    if len(DEBUG_LOGS) > 200: DEBUG_LOGS.pop(0)

def bot_thread():
    # AUTO-RECONNECT LOOP
    while BOT["should_run"]:
        try:
            BOT["status"] = "CONNECTING"
            ws = websocket.WebSocketApp(
                "wss://chatp.net:5333/server",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            BOT["ws"] = ws
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            save_debug("ERROR", f"Connection Crash: {str(e)}")
        
        # Agar connection tuta (Kick hua ya Net gaya), 5 sec wait karke wapas chalega
        if BOT["should_run"]:
            BOT["status"] = "RETRYING (5s)..."
            time.sleep(5)

def on_open(ws):
    BOT["status"] = "AUTHENTICATING"
    pkt = {"handler": "login", "id": str(time.time()), "username": BOT["user"], "password": BOT["pass"], "platform": "web"}
    ws.send(json.dumps(pkt))
    save_debug("OUT", pkt)
    
    # Background Tasks
    threading.Thread(target=pinger, daemon=True).start()
    threading.Thread(target=idle_game_checker, daemon=True).start()

def pinger():
    while BOT["ws"] and BOT["ws"].sock and BOT["ws"].sock.connected:
        time.sleep(20)
        try: BOT["ws"].send(json.dumps({"handler": "ping"}))
        except: break

def on_message(ws, message):
    try:
        data = json.loads(message)
        
        # Cache Avatar
        if data.get("avatar_url") and data.get("from"): 
            BOT["avatars"][data["from"]] = data["avatar_url"]

        if data.get("handler") not in ["receipt_ack", "ping"]: 
            save_debug("IN", data)

        if data.get("handler") == "login_event":
            if data["type"] == "success":
                BOT["status"] = "ONLINE"
                pkt = {"handler": "room_join", "id": str(time.time()), "name": BOT["room"]}
                ws.send(json.dumps(pkt))
                save_debug("OUT", pkt)
            else:
                BOT["status"] = "LOGIN FAILED"
                BOT["should_run"] = False
                ws.close()
                
        elif data.get("handler") == "room_event" and data.get("type") == "text":
            av = BOT["avatars"].get(data['from'], "https://cdn-icons-png.flaticon.com/512/149/149071.png")
            save_chat(data['from'], data['body'], av, "text")
            
            # Game Logic Call
            game_engine(data['from'], data['body'])
            
    except Exception as e: save_debug("ERROR", str(e))

def on_error(ws, error): save_debug("WS ERROR", str(error))
def on_close(ws, c, m): BOT["status"] = "DISCONNECTED"

def send_msg(text, type="text", url=""):
    if BOT["ws"]:
        pkt = {"handler": "room_message", "id": str(time.time()), "room": BOT["room"], "type": type, "body": text, "url": url, "length": "0"}
        try:
            BOT["ws"].send(json.dumps(pkt))
            bot_av = "https://cdn-icons-png.flaticon.com/512/4712/4712035.png"
            display = "[IMAGE SENT]" if type == "image" else text
            save_chat("TitanBot", display, bot_av, "bot")
            save_debug("OUT", pkt)
        except: pass

# =============================================================================
# 5. GAME ENGINE (LOCKED & SAFE)
# =============================================================================
ACTIVE_GAMES = {}

def idle_game_checker():
    while BOT["should_run"]:
        time.sleep(5)
        now = time.time()
        to_remove = []
        
        with GAME_LOCK: # Reading shared state safely
            for host, game in ACTIVE_GAMES.items():
                if now - game['last_active'] > 30: # 30 Sec Timeout
                    to_remove.append(host)
        
        for host in to_remove:
            game = None
            with GAME_LOCK:
                if host in ACTIVE_GAMES:
                    game = ACTIVE_GAMES.pop(host)
            
            if game:
                # Refund logic
                bet = game.get("bet", 0)
                if bet > 0:
                    update_score(game['p1'], bet)
                    if game['p2'] and "Bot" not in game['p2']: update_score(game['p2'], bet)
                
                send_msg(f"🛑 **TIMEOUT!** Game hosted by {host} stopped. Bets refunded.")

def game_engine(user, msg):
    msg = msg.strip().lower()
    
    # --- COMMANDS ---
    if msg == "!help":
        send_msg("🎮 **COMMANDS:**\n• `!start`\n• `!start bet 100`\n• `!sg`\n• `!join <host>`\n• `!score`\n• `1-9` (Move)")
        return

    if msg == "!score":
        bal = get_score(user)
        domain = BOT.get('domain', '')
        link = f"\n🏆 Rank: {domain}leaderboard" if domain else ""
        send_msg(f"💳 {user}: **{bal}** pts{link}")
        return

    # --- START ---
    if msg.startswith("!start"):
        with GAME_LOCK:
            if find_user_game_unsafe(user): 
                return send_msg(f"⚠ {user}, finish current game!")
        
        mode = "bot" if "bot" in msg else "pvp"
        bet = 0
        if "bet" in msg:
            try: bet = int(msg.split("bet")[1].split()[0])
            except: pass
        
        if bet > 0:
            if get_score(user) < bet: return send_msg("⚠ Low Balance!")
            update_score(user, -bet) # Deduct
            
        with GAME_LOCK:
            ACTIVE_GAMES[user] = {
                "host": user, "mode": mode, "board": [" "]*9, "turn": "X",
                "p1": user, "p2": "🤖 TitanBot" if mode=="bot" else None,
                "last_active": time.time(), "bet": bet
            }
        
        send_board(user)
        bet_txt = f" (Bet: {bet})" if bet else ""
        if mode=="pvp": send_msg(f"🎮 **PvP LOBBY{bet_txt}**\nHost: {user}\nWaiting: `!join {user}`")
        else: send_msg(f"🤖 **BOT MATCH**\n{user} vs TitanBot")
        return

    # --- JOIN ---
    if msg.startswith("!join"):
        with GAME_LOCK:
            if find_user_game_unsafe(user): return send_msg("⚠ You are playing.")
        
        parts = msg.split()
        if len(parts) < 2: return send_msg("Usage: `!join <host>`")
        host_target = parts[1]
        
        game = None
        target_host_key = None
        
        with GAME_LOCK:
            for h in ACTIVE_GAMES:
                if h.lower() == host_target.lower(): 
                    game = ACTIVE_GAMES[h]
                    target_host_key = h
                    break
        
        if not game: return send_msg("⚠ Game not found.")
        if game["mode"] == "bot" or game["p2"]: return send_msg("⚠ Full/Bot.")
        
        bet = game.get("bet", 0)
        if bet > 0:
            if get_score(user) < bet: return send_msg("⚠ Need funds.")
            update_score(user, -bet) # Deduct

        with GAME_LOCK:
            ACTIVE_GAMES[target_host_key]["p2"] = user
            ACTIVE_GAMES[target_host_key]["last_active"] = time.time()
            
        pot = f"\n💰 Pot: {bet*2}" if bet else ""
        send_msg(f"⚔ **MATCH ON!**\n{user} (O) joined {game['p1']}.{pot}")
        return

    # --- STOP ---
    if msg == "!stop":
        with GAME_LOCK:
            game = find_user_game_unsafe(user)
            if game:
                bet = game.get("bet", 0)
                # Refund
                if bet > 0:
                    threading.Thread(target=update_score, args=(game['p1'], bet)).start()
                    if game['p2'] and "Bot" not in game['p2']: 
                        threading.Thread(target=update_score, args=(game['p2'], bet)).start()
                
                del ACTIVE_GAMES[game['host']]
                send_msg("🛑 Stopped & Refunded.")
        return

    # --- MOVES ---
    if msg.isdigit():
        move = int(msg)
        game = None
        
        with GAME_LOCK:
            game = find_user_game_unsafe(user)
        
        if not game or move < 1 or move > 9: return
        
        # Update Timer
        game["last_active"] = time.time()
        
        curr = game["p1"] if game["turn"] == "X" else game["p2"]
        if game["mode"] == "bot":
            if user != game["p1"] or game["turn"] == "O": return
        elif user != curr: return
        
        idx = move - 1
        if game["board"][idx] != " ": return send_msg("⚠ Taken!")
        
        game["board"][idx] = game["turn"]
        
        if process_turn(game, user): return
        
        if game["mode"] == "bot":
            game["turn"] = "O"
            threading.Timer(1.0, run_bot, args=[game['host']]).start()
        else:
            game["turn"] = "O" if game["turn"] == "X" else "X"
            send_board(game['host'])

# Helper function (Must be used inside a LOCK or knowing it's read-only)
def find_user_game_unsafe(u):
    for h, d in ACTIVE_GAMES.items():
        if d['p1'] == u or d['p2'] == u: return d
    return None

def run_bot(host):
    with GAME_LOCK:
        if host not in ACTIVE_GAMES: return
        game = ACTIVE_GAMES[host]
        b = game["board"]
        avail = [i for i,x in enumerate(b) if x == " "]
        if not avail: return
        
        # Logic
        move = None
        wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
        for s in ["O", "X"]:
            for x,y,z in wins:
                if b[x]==s and b[y]==s and b[z]==" ": move=z; break
                if b[x]==s and b[z]==s and b[y]==" ": move=y; break
                if b[y]==s and b[z]==s and b[x]==" ": move=x; break
            if move: break
        if not move: move = random.choice(avail)
        
        game["board"][move] = "O"
    
    # Process turn releases lock internally
    if process_turn(game, "TitanBot"): return
    
    with GAME_LOCK:
        if host in ACTIVE_GAMES:
            ACTIVE_GAMES[host]["turn"] = "X"
            send_board(host)

def process_turn(game, mover):
    b = game["board"]
    win = None
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for x,y,z in wins:
        if b[x]==b[y]==b[z] and b[x]!=" ": win=f"{x},{y},{z}"; break
        
    host = game['host']
    bet = game.get("bet", 0)
    
    if win:
        send_board(host, win)
        prize = ""
        if "Bot" not in mover:
            amt = bet * 2 if bet > 0 else 50
            # DB Update (Safe)
            av = BOT["avatars"].get(mover, "")
            update_score(mover, amt, av)
            prize = f" (+{amt} pts)"
            
        send_msg(f"🏆 **{mover} WINS!**{prize}")
        
        with GAME_LOCK:
            if host in ACTIVE_GAMES: del ACTIVE_GAMES[host]
        return True
        
    elif " " not in b:
        send_board(host)
        if bet > 0:
            # Refund
            update_score(game['p1'], bet)
            if game['p2'] and "Bot" not in game['p2']: update_score(game['p2'], bet)
            
        send_msg(f"🤝 **DRAW!** Refunded.")
        with GAME_LOCK:
            if host in ACTIVE_GAMES: del ACTIVE_GAMES[host]
        return True
    return False

def send_board(host, line=""):
    # Read-only access to game state is fine here
    game = ACTIVE_GAMES.get(host)
    if not game: return
    b_str = "".join(game["board"]).replace(" ", "_")
    base = BOT.get('domain', '')
    if not base: return
    url = f"{base}render?b={b_str}&w={line}&h={host}&t={int(time.time())}"
    send_msg("", "image", url)

# =============================================================================
# 6. FLASK ROUTES
# =============================================================================
@app.route('/')
def index(): return render_template_string(UI_TEMPLATE)

@app.route('/leaderboard')
def leaderboard():
    users = get_leaderboard_data()
    return render_template_string(LEADERBOARD_TEMPLATE, users=users)

@app.route('/connect', methods=['POST'])
def connect():
    if BOT["should_run"]: return jsonify({"status": "Already Running"})
    d = request.json
    BOT.update({"user": d['u'], "pass": d['p'], "room": d['r'], "should_run": True, "domain": request.url_root})
    threading.Thread(target=bot_thread, daemon=True).start()
    return jsonify({"status": "Starting..."})

@app.route('/disconnect', methods=['POST'])
def disconnect():
    BOT["should_run"] = False
    if BOT["ws"]: BOT["ws"].close()
    return jsonify({"status": "Stopped"})

@app.route('/clear_data', methods=['POST'])
def clear_data():
    CHAT_HISTORY.clear()
    DEBUG_LOGS.clear()
    return jsonify({"status": "Cleared"})

@app.route('/get_data')
def get_data():
    return jsonify({"status": BOT["status"], "chat": CHAT_HISTORY, "debug": DEBUG_LOGS})

@app.route('/render')
def render():
    try:
        b_str = request.args.get('b', '_________')
        w_line = request.args.get('w', '')
        if 'board' not in ASSETS: init_assets()
        base = ASSETS['board'].copy()
        x_img, o_img = ASSETS['x'], ASSETS['o']
        for i, c in enumerate(b_str):
            if c in ['X', 'O']:
                sym = x_img if c == 'X' else o_img
                base.paste(sym, ((i%3)*300, (i//3)*300), sym)
        if w_line:
            draw = ImageDraw.Draw(base)
            idx = [int(k) for k in w_line.split(',')]
            s, e = idx[0], idx[2]
            draw.line([((s%3)*300+150, (s//3)*300+150), ((e%3)*300+150, (e//3)*300+150)], fill="#ffd700", width=25)
        img_io = io.BytesIO()
        base.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except: return "Error", 500

# =============================================================================
# 7. UI TEMPLATES (ADVANCED)
# =============================================================================

LEADERBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>TITAN RANKINGS</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@600&display=swap" rel="stylesheet">
    <style>
        body { background: #050505; color: #fff; font-family: 'Rajdhani', sans-serif; margin: 0; padding: 20px; }
        h1 { text-align: center; color: #00f3ff; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 30px; }
        .card {
            background: #111; border: 1px solid #333; border-radius: 12px;
            padding: 15px; margin-bottom: 12px; display: flex; align-items: center;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5); transition: transform 0.2s;
        }
        .card:hover { transform: scale(1.02); border-color: #00f3ff; }
        
        .rank { font-size: 24px; width: 50px; text-align: center; font-weight: bold; color: #555; }
        .rank-1 { color: #FFD700; text-shadow: 0 0 10px #FFD700; font-size: 30px; }
        .rank-2 { color: #C0C0C0; text-shadow: 0 0 10px #C0C0C0; font-size: 28px; }
        .rank-3 { color: #CD7F32; text-shadow: 0 0 10px #CD7F32; font-size: 26px; }
        
        .avatar { width: 55px; height: 55px; border-radius: 50%; background: #222; margin-right: 15px; border: 2px solid #333; object-fit: cover; }
        .rank-1 + .avatar { border-color: #FFD700; }
        
        .info { flex: 1; }
        .name { font-size: 18px; font-weight: bold; display: block; color: #e0e0e0; }
        .stats { font-size: 14px; color: #888; }
        .score { font-size: 20px; color: #00ff41; font-weight: bold; text-shadow: 0 0 5px rgba(0,255,65,0.3); }
    </style>
</head>
<body>
    <h1>🏆 Global Legends</h1>
    {% for u in users %}
    <div class="card">
        <div class="rank {% if loop.index == 1 %}rank-1{% elif loop.index == 2 %}rank-2{% elif loop.index == 3 %}rank-3{% endif %}">
            {% if loop.index == 1 %}🥇{% elif loop.index == 2 %}🥈{% elif loop.index == 3 %}🥉{% else %}#{{loop.index}}{% endif %}
        </div>
        <img class="avatar" src="{{ u[3] if u[3] else 'https://cdn-icons-png.flaticon.com/512/149/149071.png' }}">
        <div class="info">
            <span class="name">{{ u[0] }}</span>
            <span class="stats">Wins: {{ u[2] }}</span>
        </div>
        <div class="score">{{ u[1] }}</div>
    </div>
    {% endfor %}
</body>
</html>
"""

UI_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>TITAN OS</title>
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #050505; --panel: #111; --primary: #00f3ff; --accent: #ff003c; --text: #e0e6ed;
            --safe-bottom: env(safe-area-inset-bottom, 20px);
        }
        * { box-sizing: border-box; }
        body { 
            margin: 0; background: var(--bg); color: var(--text); font-family: 'Rajdhani', sans-serif; 
            height: 100dvh; width: 100vw; overflow: hidden; display: flex; flex-direction: column;
        }
        
        /* GRID BG */
        body::before {
            content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            background-image: linear-gradient(rgba(0, 243, 255, 0.05) 1px, transparent 1px),
                              linear-gradient(90deg, rgba(0, 243, 255, 0.05) 1px, transparent 1px);
            background-size: 30px 30px; z-index: -1;
        }

        /* LOGIN */
        #login-view {
            position: absolute; top:0; left:0; width:100%; height:100%; z-index: 999;
            display: flex; justify-content: center; align-items: center; background: #000;
        }
        .login-card { width: 85%; max-width: 320px; padding: 25px; background: #111; border: 1px solid #333; border-radius: 12px; text-align: center; }
        .logo { font-size: 24px; color: var(--primary); margin-bottom: 20px; font-weight:bold; letter-spacing:2px; }
        input { width: 100%; padding: 12px; margin-bottom: 12px; background: #000; border: 1px solid #333; color: #fff; border-radius: 6px; font-family: 'JetBrains Mono'; }
        .btn-login { width: 100%; padding: 12px; background: var(--primary); color: #000; font-weight: bold; border: none; border-radius: 6px; cursor: pointer; }

        /* APP */
        #app-view { display: none; height: 100%; flex-direction: column; width: 100%; }
        
        header {
            height: 50px; flex-shrink: 0; background: rgba(10,12,18,0.95); border-bottom: 1px solid #333;
            display: flex; align-items: center; justify-content: space-between; padding: 0 15px;
        }
        .status-badge { font-size: 10px; padding: 3px 8px; border-radius: 4px; background: #222; color: #666; font-weight: bold; border: 1px solid #444; }
        .status-online { background: rgba(0,255,0,0.1); color: #0f0; border-color: #0f0; }

        .content { flex: 1; position: relative; overflow: hidden; background: rgba(0,0,0,0.3); }
        .tab-content { position: absolute; top:0; left:0; width:100%; height:100%; display: none; flex-direction: column; }
        .active-tab { display: flex; }

        /* CHAT */
        #chat-container { flex: 1; overflow-y: auto; padding: 15px; display: flex; flex-direction: column; gap: 10px; padding-bottom: 20px; }
        .msg-row { display: flex; gap: 10px; max-width: 90%; }
        .msg-right { align-self: flex-end; flex-direction: row-reverse; }
        .avatar { width: 32px; height: 32px; border-radius: 50%; background: #222; flex-shrink: 0; border:1px solid #333; object-fit: cover; }
        .bubble { background: #1e1e1e; padding: 8px 12px; border-radius: 12px; border-top-left-radius: 2px; font-size: 13px; line-height: 1.4; color: #ddd; word-wrap: break-word; }
        .msg-right .bubble { background: #004d40; border-top-left-radius: 12px; border-top-right-radius: 2px; }

        /* DEBUG */
        .debug-toolbar { padding: 8px; background: #111; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
        .debug-logs { flex: 1; overflow-y: auto; padding: 10px; font-family: 'JetBrains Mono', monospace; font-size: 10px; }
        .log-entry { margin-bottom: 8px; border-bottom: 1px solid #222; padding-bottom: 4px; }
        .dir-IN { color: #0f0; } .dir-OUT { color: #0ff; } .dir-ERROR { color: #f00; }
        pre { margin: 0; white-space: pre-wrap; word-break: break-all; }
        
        /* LEADERBOARD FRAME */
        iframe { width: 100%; height: 100%; border: none; }

        /* NAV */
        nav { height: 55px; flex-shrink: 0; background: #000; border-top: 1px solid #333; display: flex; padding-bottom: var(--safe-bottom); }
        .nav-btn { flex: 1; background: transparent; border: none; color: #555; font-weight: bold; font-size: 12px; border-top: 2px solid transparent; cursor: pointer; }
        .nav-btn.active { color: #fff; border-top-color: var(--primary); background: #0a0a0a; }
        
        /* UTILS */
        .btn-sm { font-size:10px; background:#333; color:#fff; border:none; padding:4px 8px; border-radius:3px; cursor: pointer; }
        .btn-hl { background:var(--primary); color:#000; font-weight:bold; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 2px; }
    </style>
</head>
<body>

    <div id="login-view">
        <div class="login-card">
            <div class="logo">TITAN OS</div>
            <input id="u" placeholder="Username"><input id="p" type="password" placeholder="Password"><input id="r" placeholder="Room Name">
            <button onclick="login()" class="btn-login">CONNECT</button>
        </div>
    </div>

    <div id="app-view" style="display:none">
        <header>
            <div style="font-weight:bold;color:#fff">TITAN PANEL</div>
            <div style="display:flex;gap:10px;align-items:center">
                <div id="status-badge" class="status-badge">OFFLINE</div>
                <button onclick="logout()" class="btn-sm" style="border:1px solid #f03;background:none;color:#f03">EXIT</button>
            </div>
        </header>

        <div class="content">
            <!-- CHAT -->
            <div id="tab-chat" class="tab-content active-tab">
                <div id="chat-container"></div>
            </div>

            <!-- LEADERBOARD -->
            <div id="tab-lb" class="tab-content">
                <iframe src="/leaderboard"></iframe>
            </div>

            <!-- DEBUG -->
            <div id="tab-debug" class="tab-content">
                <div class="debug-toolbar">
                    <span style="color:#666;font-size:11px">LOG STREAM</span>
                    <div style="display:flex;gap:5px">
                        <button onclick="copyLogs()" class="btn-sm">COPY</button>
                        <button onclick="clearData()" class="btn-sm">CLEAR</button>
                        <button onclick="downloadLogs()" class="btn-sm btn-hl">SAVE</button>
                    </div>
                </div>
                <div id="debug-log-area" class="debug-logs"></div>
            </div>
        </div>

        <nav>
            <button onclick="switchTab('chat')" id="btn-chat" class="nav-btn active">CHAT</button>
            <button onclick="switchTab('lb')" id="btn-lb" class="nav-btn">RANKS</button>
            <button onclick="switchTab('debug')" id="btn-debug" class="nav-btn">DEBUG</button>
        </nav>
    </div>

    <script>
        let currentTab = 'chat';
        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active-tab'));
            document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active-tab');
            document.getElementById('btn-' + tab).classList.add('active');
            
            if(tab === 'chat') scrollToBottom('chat-container');
            if(tab === 'debug') scrollToBottom('debug-log-area');
            // Refresh iframe when clicking ranks
            if(tab === 'lb') document.querySelector('iframe').src = '/leaderboard';
        }

        function login() {
            const u = document.getElementById('u').value, p = document.getElementById('p').value, r = document.getElementById('r').value;
            if(!u || !p || !r) return alert("Missing info");
            document.getElementById('login-view').style.display = 'none';
            document.getElementById('app-view').style.display = 'flex';
            fetch('/connect', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({u, p, r})});
        }

        function logout() { fetch('/disconnect', {method: 'POST'}); location.reload(); }
        function clearData() { fetch('/clear_data', {method: 'POST'}); }

        function scrollToBottom(id) {
            const el = document.getElementById(id);
            if(el) el.scrollTop = el.scrollHeight;
        }
        
        function copyLogs() {
            const text = document.getElementById('debug-log-area').innerText;
            navigator.clipboard.writeText(text).then(() => alert("Logs Copied!"));
        }

        function downloadLogs() {
            const el = document.getElementById('debug-log-area');
            const blob = new Blob([el.innerText], { type: 'text/plain' });
            const a = document.createElement('a');
            a.href = window.URL.createObjectURL(blob);
            a.download = 'titan_logs.txt';
            a.click();
        }

        setInterval(() => {
            fetch('/get_data').then(r=>r.json()).then(d => {
                const badge = document.getElementById('status-badge');
                badge.innerText = d.status;
                badge.className = d.status === 'ONLINE' ? 'status-badge status-online' : 'status-badge';

                const chatDiv = document.getElementById('chat-container');
                const chatHtml = d.chat.map(m => `
                    <div class="msg-row ${m.type === 'bot' ? 'msg-right' : 'msg-left'}">
                        <img src="${m.avatar}" class="avatar" onerror="this.src='https://cdn-icons-png.flaticon.com/512/149/149071.png'">
                        <div class="bubble"><b>${m.user}</b><br>${m.msg}</div>
                    </div>`).join('');
                
                if(chatDiv.innerHTML.length !== chatHtml.length) { 
                    chatDiv.innerHTML = chatHtml; 
                    if(currentTab==='chat') scrollToBottom('chat-container'); 
                }

                const debugDiv = document.getElementById('debug-log-area');
                const visibleLogs = d.debug.slice(-50); // Show last 50 for performance
                const debugHtml = visibleLogs.map(l => `
                    <div class="log-entry">
                        <span style="opacity:0.5">[${l.time}]</span> <span class="dir-${l.dir}">${l.dir}</span>
                        <pre style="color:#aaa;margin:0">${JSON.stringify(l.data)}</pre>
                    </div>`).join('');
                
                if(debugDiv.innerHTML.length !== debugHtml.length) { 
                    debugDiv.innerHTML = debugHtml; 
                    if(currentTab==='debug') scrollToBottom('debug-log-area'); 
                }
            });
        }, 1000);
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
