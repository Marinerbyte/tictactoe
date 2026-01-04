import os
import json
import time
import threading
import io
import random
import requests
import websocket
import psycopg2
import sqlite3
from flask import Flask, render_template_string, request, jsonify, send_file
from PIL import Image, ImageDraw

app = Flask(__name__)

# =============================================================================
# 1. ASSET GENERATION SYSTEM (IN-MEMORY)
# =============================================================================
# Ye system bot start hote hi RAM mein images bana lega.
# File save karne ki zarurat nahi padegi.

ASSETS = {}

def init_assets():
    print(">> Generating Assets in Memory...")
    
    # 1. BOARD (Dark Gray with Neon Cyan Grid)
    board = Image.new('RGB', (900, 900), (25, 25, 30)) 
    draw = ImageDraw.Draw(board)
    grid_color = (0, 255, 255) # Cyan
    
    # Outer Border
    draw.rectangle([10, 10, 890, 890], outline=grid_color, width=15)
    # Grid Lines
    for xy in [300, 600]:
        draw.line([(xy, 20), (xy, 880)], fill=grid_color, width=15)
        draw.line([(20, xy), (880, xy)], fill=grid_color, width=15)
    
    ASSETS['board'] = board

    # 2. X (Red Neon)
    x_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_x = ImageDraw.Draw(x_img)
    d_x.line([(50,50), (250,250)], fill=(255, 50, 50), width=25)
    d_x.line([(250,50), (50,250)], fill=(255, 50, 50), width=25)
    ASSETS['x'] = x_img

    # 3. O (Green Neon)
    o_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_o = ImageDraw.Draw(o_img)
    d_o.ellipse([50,50,250,250], outline=(50, 255, 50), width=25)
    ASSETS['o'] = o_img
    
    print(">> Assets Ready (RAM).")

# Initialize immediately
init_assets()

# =============================================================================
# 2. CONFIG & DATABASE
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_SQLITE = False if DATABASE_URL else True
DB_FILE = "tictactoe.db"

def get_db():
    if USE_SQLITE: return sqlite3.connect(DB_FILE)
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS players 
                   (username VARCHAR(255) PRIMARY KEY, wins INTEGER, score INTEGER)''')
        conn.commit()
        conn.close()
    except: pass

def add_win(username, points=50):
    try:
        conn = get_db()
        c = conn.cursor()
        ph = "?" if USE_SQLITE else "%s"
        c.execute(f"SELECT wins, score FROM players WHERE username={ph}", (username,))
        data = c.fetchone()
        if data:
            c.execute(f"UPDATE players SET wins={ph}, score={ph} WHERE username={ph}", (data[0]+1, data[1]+points, username))
        else:
            c.execute(f"INSERT INTO players (username, wins, score) VALUES ({ph}, 1, {ph})", (username, points))
        conn.commit()
        conn.close()
    except: pass

init_db()

# =============================================================================
# 3. CONNECTION LOGIC
# =============================================================================

BOT = {"ws": None, "connected": False, "user": "", "pass": "", "room": "", "domain": ""}
LOGS = []

def log(msg, type="sys"):
    LOGS.append({"time": time.strftime("%H:%M:%S"), "msg": msg, "type": type})
    if len(LOGS) > 50: LOGS.pop(0)

def on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("handler") == "room_event" and data.get("type") == "text":
            game_engine(data['from'], data['body'])
        elif data.get("handler") == "login_event":
            if data["type"] == "success":
                ws.send(json.dumps({"handler": "room_join", "id": str(time.time()), "name": BOT["room"]}))
            else: BOT["connected"] = False
    except: pass

def on_error(ws, error): log(f"Error: {error}", "err")
def on_close(ws, c, m): BOT["connected"] = False

def on_open(ws):
    BOT["connected"] = True
    ws.send(json.dumps({"handler": "login", "id": str(time.time()), "username": BOT["user"], "password": BOT["pass"], "platform": "web"}))
    threading.Thread(target=pinger, daemon=True).start()

def pinger():
    while BOT["connected"]:
        time.sleep(20)
        try: BOT["ws"].send(json.dumps({"handler": "ping"}))
        except: break

def send_msg(text, type="text", url=""):
    if BOT["ws"] and BOT["connected"]:
        try:
            BOT["ws"].send(json.dumps({
                "handler": "room_message", "id": str(time.time()), 
                "room": BOT["room"], "type": type, "body": text, "url": url, "length": "0"
            }))
        except: pass

# =============================================================================
# 4. MULTI-INSTANCE GAME ENGINE
# =============================================================================

ACTIVE_GAMES = {}

def game_engine(user, msg):
    msg = msg.strip().lower()
    
    if msg == "!help":
        send_msg("🎮 **COMMANDS:**\n• `!start` -> Start PvP\n• `!start bot` -> Play Solo\n• `!join <user>` -> Join Game\n• `!stop` -> End Game\n• `1-9` -> Move")
        return

    # START
    if msg.startswith("!start"):
        if find_user_game(user):
            send_msg(f"⚠ {user}, you are already playing! Type !stop to end it.")
            return

        mode = "bot" if "bot" in msg else "pvp"
        ACTIVE_GAMES[user] = {
            "host": user, "mode": mode, "board": [" "] * 9, "turn": "X",
            "p1": user, "p2": "🤖 TitanBot" if mode == "bot" else None
        }
        
        send_board(user)
        if mode == "pvp": send_msg(f"🎮 **PvP LOBBY**\nHost: {user}\nOpponent: Type `!join {user}`")
        else: send_msg(f"🤖 **BOT MATCH**\n{user} vs TitanBot")
        return

    # JOIN
    if msg.startswith("!join"):
        if find_user_game(user): return send_msg(f"⚠ {user}, you are already playing.")
        parts = msg.split()
        if len(parts) < 2: return send_msg("⚠ Usage: `!join <host_name>`")
        
        host_name = parts[1]
        game = ACTIVE_GAMES.get(host_name)
        # Case insensitive search
        if not game:
            for h in ACTIVE_GAMES:
                if h.lower() == host_name.lower(): game = ACTIVE_GAMES[h]; break
        
        if not game: return send_msg(f"⚠ No game found hosted by {host_name}.")
        if game["mode"] == "bot": return send_msg("⚠ Cannot join a Bot game.")
        if game["p2"]: return send_msg("⚠ Game full!")

        game["p2"] = user
        send_msg(f"⚔ **MATCH ON!**\n{user} (O) joined {game['p1']}!")
        return

    # STOP
    if msg == "!stop":
        game = find_user_game(user)
        if game:
            del ACTIVE_GAMES[game['host']]
            send_msg(f"🛑 Game hosted by {game['host']} stopped.")
        else: send_msg("⚠ You are not in a game.")
        return

    # MOVES
    if msg.isdigit():
        move = int(msg)
        if move < 1 or move > 9: return
        game = find_user_game(user)
        if not game: return

        curr = game["p1"] if game["turn"] == "X" else game["p2"]
        if game["mode"] == "bot":
            if user != game["p1"] or game["turn"] == "O": return
        elif user != curr: return

        idx = move - 1
        if game["board"][idx] != " ": return send_msg(f"⚠ Taken!")

        game["board"][idx] = game["turn"]
        if process_turn(game, user): return

        if game["mode"] == "bot":
            game["turn"] = "O"
            threading.Timer(1.0, execute_bot, args=[game['host']]).start()
        else:
            game["turn"] = "O" if game["turn"] == "X" else "X"
            send_board(game['host'])

def find_user_game(u):
    for h, d in ACTIVE_GAMES.items():
        if d['p1'] == u or d['p2'] == u: return d
    return None

def execute_bot(host):
    game = ACTIVE_GAMES.get(host)
    if not game: return
    b = game["board"]
    avail = [i for i, x in enumerate(b) if x == " "]
    if not avail: return

    move = find_best(b, "O")
    if move is None: move = find_best(b, "X")
    if move is None: move = random.choice(avail)

    game["board"][move] = "O"
    if process_turn(game, "TitanBot"): return
    game["turn"] = "X"
    send_board(host)

def find_best(b, s):
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for x,y,z in wins:
        if b[x]==s and b[y]==s and b[z]==" ": return z
        if b[x]==s and b[z]==s and b[y]==" ": return y
        if b[y]==s and b[z]==s and b[x]==" ": return x
    return None

def process_turn(game, mover):
    winner = check_win(game["board"])
    host = game['host']
    if winner:
        send_board(host, winner['line'])
        if "Bot" not in mover: add_win(mover)
        send_msg(f"🏆 **{mover} WINS!**")
        del ACTIVE_GAMES[host]
        return True
    elif " " not in game["board"]:
        send_board(host)
        send_msg("🤝 **DRAW!**")
        del ACTIVE_GAMES[host]
        return True
    return False

def check_win(b):
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for x,y,z in wins:
        if b[x]==b[y]==b[z] and b[x]!=" ": return {'line': f"{x},{y},{z}"}
    return None

def send_board(host, win_line=""):
    game = ACTIVE_GAMES.get(host)
    if not game: return
    b_str = "".join(game["board"]).replace(" ", "_")
    url = f"{BOT['domain']}render?b={b_str}&w={win_line}&h={host}&t={int(time.time())}"
    send_msg("", "image", url)

# =============================================================================
# 5. ROUTES & RENDERER (Uses Memory Assets)
# =============================================================================

@app.route('/')
def index(): return render_template_string(HTML, c=BOT["connected"])

@app.route('/connect', methods=['POST'])
def connect():
    if BOT["connected"]: return jsonify({"status": "Connected"})
    d = request.json
    BOT.update({"user": d['u'], "pass": d['p'], "room": d['r'], "domain": request.url_root})
    threading.Thread(target=lambda: websocket.WebSocketApp("wss://chatp.net:5333/server", on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close).run_forever()).start()
    return jsonify({"status": "Connecting..."})

@app.route('/disconnect', methods=['POST'])
def disconnect():
    if BOT["ws"]: BOT["ws"].close()
    return jsonify({"status": "Disconnected"})

@app.route('/logs')
def get_logs(): return jsonify({"logs": LOGS, "connected": BOT["connected"]})

@app.route('/render')
def render():
    try:
        b_str = request.args.get('b', '_________')
        w_line = request.args.get('w', '')
        
        # --- KEY CHANGE: Use ASSETS from RAM instead of file ---
        # We use .copy() so we don't draw on the master template
        if 'board' not in ASSETS: init_assets() # Failsafe
        
        base = ASSETS['board'].copy()
        x_img = ASSETS['x']
        o_img = ASSETS['o']

        for i, c in enumerate(b_str):
            if c in ['X', 'O']:
                sym = x_img if c == 'X' else o_img
                base.paste(sym, ((i%3)*300, (i//3)*300), sym)
        
        if w_line:
            draw = ImageDraw.Draw(base)
            idx = [int(k) for k in w_line.split(',')]
            start, end = idx[0], idx[2]
            draw.line([((start%3)*300+150, (start//3)*300+150), ((end%3)*300+150, (end//3)*300+150)], fill="yellow", width=25)

        img_io = io.BytesIO()
        base.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e: return str(e), 500

HTML = """
<!DOCTYPE html><html><body style="background:#000;color:#0f0;font-family:monospace;text-align:center">
<h2>TITAN AUTO-ASSET BOT</h2><div id="s" style="color:red">OFFLINE</div>
<input id="u" placeholder="User"><input id="p" type="password" placeholder="Pass"><input id="r" placeholder="Room">
<br><button onclick="c()">Connect</button><button onclick="d()">Disconnect</button>
<div id="l" style="border:1px solid #333;height:200px;overflow:auto;margin-top:20px;text-align:left"></div>
<script>
function c(){fetch('/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({u:document.getElementById('u').value,p:document.getElementById('p').value,r:document.getElementById('r').value})})}
function d(){fetch('/disconnect',{method:'POST'})}
setInterval(()=>{fetch('/logs').then(r=>r.json()).then(d=>{
document.getElementById('s').innerText=d.connected?"ONLINE":"OFFLINE";document.getElementById('s').style.color=d.connected?"#0f0":"red";
document.getElementById('l').innerHTML=d.logs.map(x=>`<div>[${x.time}] ${x.msg}</div>`).join('')})},1000)
</script></body></html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
