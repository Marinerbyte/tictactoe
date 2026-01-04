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
# 1. DATABASE & CONFIG
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_SQLITE = False if DATABASE_URL else True
DB_FILE = "tictactoe.db"

def get_db():
    if USE_SQLITE:
        return sqlite3.connect(DB_FILE)
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS players 
                   (username VARCHAR(255) PRIMARY KEY, wins INTEGER, score INTEGER)''')
        conn.commit()
        conn.close()
        print(">> Database Ready.")
    except Exception as e: print(f"DB Error: {e}")

def add_win(username, points=50):
    try:
        conn = get_db()
        c = conn.cursor()
        ph = "?" if USE_SQLITE else "%s"
        c.execute(f"SELECT wins, score FROM players WHERE username={ph}", (username,))
        data = c.fetchone()
        if data:
            c.execute(f"UPDATE players SET wins={ph}, score={ph} WHERE username={ph}", 
                      (data[0]+1, data[1]+points, username))
        else:
            c.execute(f"INSERT INTO players (username, wins, score) VALUES ({ph}, 1, {ph})", 
                      (username, points))
        conn.commit()
        conn.close()
    except Exception as e: print(f"Score Error: {e}")

init_db()

# =============================================================================
# 2. BOT CONNECTION LOGIC
# =============================================================================

BOT = {
    "ws": None, "connected": False, 
    "user": "", "pass": "", "room": "", 
    "domain": ""
}

LOGS = []
def log(msg, type="sys"):
    timestamp = time.strftime("%H:%M:%S")
    LOGS.append({"time": timestamp, "msg": msg, "type": type})
    if len(LOGS) > 50: LOGS.pop(0)

def on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("handler") == "room_event" and data.get("type") == "text":
            log(f"[{data['from']}]: {data['body']}", "in")
            game_engine(data['from'], data['body'])
            
        elif data.get("handler") == "login_event":
            if data["type"] == "success":
                log("Login Success! Joining Room...", "sys")
                ws.send(json.dumps({"handler": "room_join", "id": str(time.time()), "name": BOT["room"]}))
            else:
                log(f"Login Failed: {data.get('reason')}", "err")
                BOT["connected"] = False
    except Exception as e: print(f"WS Error: {e}")

def on_error(ws, error): log(f"Error: {error}", "err")
def on_close(ws, c, m): 
    log("Disconnected.", "err")
    BOT["connected"] = False

def on_open(ws):
    BOT["connected"] = True
    log("Connected. Authenticating...", "sys")
    ws.send(json.dumps({
        "handler": "login", "id": str(time.time()), 
        "username": BOT["user"], "password": BOT["pass"], "platform": "web"
    }))
    
    def pinger():
        while BOT["connected"]:
            time.sleep(20)
            try: ws.send(json.dumps({"handler": "ping"}))
            except: break
    threading.Thread(target=pinger, daemon=True).start()

def send_msg(text, type="text", url=""):
    if BOT["ws"] and BOT["connected"]:
        try:
            BOT["ws"].send(json.dumps({
                "handler": "room_message", "id": str(time.time()), 
                "room": BOT["room"], "type": type, "body": text, "url": url, "length": "0"
            }))
            if type == "text": log(f"BOT: {text}", "out")
            else: log("BOT: Sent Image", "out")
        except: pass

# =============================================================================
# 3. GAME ENGINE (PVP & BOT MODES)
# =============================================================================

GAME = {
    "active": False,
    "mode": None,  # 'pvp' or 'bot'
    "board": [" "] * 9,
    "turn": "X",
    "p1": None, 
    "p2": None
}

def game_engine(user, msg):
    msg = msg.strip().lower()
    global GAME

    # --- HELP ---
    if msg == "!help":
        help_text = (
            "🎮 **COMMAND LIST**\n"
            "• `!start` -> Start 2-Player Game\n"
            "• `!start bot` -> Play against CPU\n"
            "• `!join` -> Join a PvP Game\n"
            "• `1-9` -> Make a move"
        )
        send_msg(help_text)

    # --- START GAME ---
    elif msg.startswith("!start"):
        if GAME["active"]: 
            return send_msg(f"⚠ Game in progress: {GAME['p1']} vs {GAME['p2'] or 'Waiting...'}")
        
        mode = "bot" if "bot" in msg else "pvp"
        
        GAME = {
            "active": True, "mode": mode,
            "board": [" "] * 9, "turn": "X",
            "p1": user, 
            "p2": "🤖 TitanBot" if mode == "bot" else None
        }
        
        send_board()
        if mode == "pvp":
            send_msg(f"🎮 **PVP MODE STARTED**\nPlayer X: {user}\nWaiting for Player 2... (Type !join)")
        else:
            send_msg(f"🤖 **BOT MODE STARTED**\nPlayer X: {user} vs TitanBot (O)\nYour turn! Type 1-9")

    # --- JOIN (Only for PvP) ---
    elif msg == "!join":
        if not GAME["active"]: return send_msg("⚠ No active game. Type !start")
        if GAME["mode"] == "bot": return send_msg("⚠ This is a Bot game. You cannot join.")
        if GAME["p2"]: return send_msg("⚠ Room is full!")
        if user == GAME["p1"]: return send_msg("⚠ You cannot play against yourself.")
        
        GAME["p2"] = user
        send_msg(f"⚔ **MATCH ON!**\n{user} (O) joined!\n{GAME['p1']} (X) is up.")

    # --- MAKE A MOVE ---
    elif msg.isdigit() and GAME["active"]:
        move = int(msg)
        if move < 1 or move > 9: return

        # 1. CHECK PLAYER TURN
        curr_player = GAME["p1"] if GAME["turn"] == "X" else GAME["p2"]
        
        # If playing against bot, ensure it's P1's turn
        if GAME["mode"] == "bot":
            if user != GAME["p1"]: return # Only P1 can command in bot mode
            if GAME["turn"] == "O": return # Wait for bot
        else:
            # PvP check
            if user != curr_player: return

        # 2. CHECK SLOT VALIDITY
        idx = move - 1
        if GAME["board"][idx] != " ": return send_msg("⚠ Spot taken!")

        # 3. EXECUTE PLAYER MOVE
        GAME["board"][idx] = GAME["turn"]
        
        if process_turn(user): return # If game ended, stop here

        # 4. BOT MOVE LOGIC (If in Bot Mode)
        if GAME["mode"] == "bot" and GAME["active"]:
            GAME["turn"] = "O"
            threading.Timer(1.0, execute_bot_move).start() # Delay for realism
        else:
            # Switch turn for PvP
            GAME["turn"] = "O" if GAME["turn"] == "X" else "X"
            send_board()

def execute_bot_move():
    global GAME
    if not GAME["active"]: return

    # --- SIMPLE AI LOGIC ---
    b = GAME["board"]
    available = [i for i, x in enumerate(b) if x == " "]
    
    if not available: return # Should not happen handled by draw check

    # 1. Try to Win
    move = find_best_move(b, "O")
    # 2. Block Player
    if move is None: move = find_best_move(b, "X")
    # 3. Take Center
    if move is None and 4 in available: move = 4
    # 4. Random
    if move is None: move = random.choice(available)

    GAME["board"][move] = "O"
    
    if process_turn("TitanBot"): return
    
    GAME["turn"] = "X"
    send_board()

def find_best_move(board, symbol):
    # Check if a move leads to a win
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for x, y, z in wins:
        if board[x] == symbol and board[y] == symbol and board[z] == " ": return z
        if board[x] == symbol and board[z] == symbol and board[y] == " ": return y
        if board[y] == symbol and board[z] == symbol and board[x] == " ": return x
    return None

def process_turn(current_user):
    # Check Win or Draw after a move
    winner = check_win()
    if winner:
        GAME["active"] = False
        send_board(win_line=winner['line'])
        if GAME["mode"] == "bot" and current_user == "TitanBot":
             send_msg(f"🤖 **TitanBot WINS!** Better luck next time.")
        else:
            add_win(current_user)
            send_msg(f"🏆 **VICTORY!**\n{current_user} WINS! (+50 Pts)")
        return True
    elif " " not in GAME["board"]:
        GAME["active"] = False
        send_board()
        send_msg("🤝 **DRAW!** Good Game.")
        return True
    return False

def check_win():
    b = GAME["board"]
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for x,y,z in wins:
        if b[x] == b[y] == b[z] and b[x] != " ":
            return {'line': f"{x},{y},{z}"}
    return None

def send_board(win_line=""):
    b_str = "".join(GAME["board"]).replace(" ", "_")
    ts = int(time.time())
    url = f"{BOT['domain']}render?b={b_str}&w={win_line}&t={ts}"
    send_msg("", type="image", url=url)

# =============================================================================
# 4. FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(HTML_DASHBOARD, connected=BOT["connected"])

@app.route('/connect', methods=['POST'])
def connect():
    if BOT["connected"]: return jsonify({"status": "Connected"})
    d = request.json
    BOT.update({"user": d['u'], "pass": d['p'], "room": d['r'], "domain": request.url_root})
    
    def run_ws():
        websocket.enableTrace(False)
        ws = websocket.WebSocketApp("wss://chatp.net:5333/server",
            on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        BOT["ws"] = ws
        ws.run_forever()
    
    threading.Thread(target=run_ws).start()
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
        board_str = request.args.get('b', '_________')
        win_line = request.args.get('w', '')
        
        try:
            base = Image.open("board.png").convert("RGBA")
            x_img = Image.open("x.png").convert("RGBA")
            o_img = Image.open("o.png").convert("RGBA")
        except: return "Missing Assets! Run create_assets.py", 500

        for i, char in enumerate(board_str):
            if char in ['X', 'O']:
                row, col = i // 3, i % 3
                x, y = col * 300, row * 300
                symbol = x_img if char == 'X' else o_img
                base.paste(symbol, (x, y), symbol)

        if win_line:
            draw = ImageDraw.Draw(base)
            idx = [int(k) for k in win_line.split(',')]
            start, end = idx[0], idx[2]
            
            x1 = (start % 3) * 300 + 150
            y1 = (start // 3) * 300 + 150
            x2 = (end % 3) * 300 + 150
            y2 = (end // 3) * 300 + 150
            
            draw.line([(x1, y1), (x2, y2)], fill="#ffd700", width=25)

        img_io = io.BytesIO()
        base.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e: return str(e), 500

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <title>TITAN TTT BOT</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background: #0a0a0a; color: #00f3ff; font-family: monospace; display: flex; flex-direction: column; align-items: center; min-height: 100vh; margin: 0; }
        .box { background: #111; padding: 20px; border: 1px solid #333; width: 90%; max-width: 400px; margin-top: 20px; box-shadow: 0 0 20px rgba(0, 243, 255, 0.1); }
        input { width: 100%; background: #000; border: 1px solid #444; color: #fff; padding: 10px; margin: 5px 0; box-sizing: border-box; }
        button { width: 100%; padding: 10px; font-weight: bold; cursor: pointer; border: none; margin-top: 10px; }
        .btn-on { background: #00ff41; color: #000; }
        .btn-off { background: #ff003c; color: #fff; }
        #logs { height: 200px; overflow-y: auto; background: #000; border: 1px solid #333; padding: 10px; font-size: 12px; margin-top: 20px; }
        .sys { color: #888; } .in { color: #00ff41; } .out { color: #00f3ff; } .err { color: #ff003c; }
        .status { text-align: center; margin-bottom: 10px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="box">
        <h2 style="text-align:center; margin-top:0;">⭕ TITAN BOT ❌</h2>
        <div id="status" class="status" style="color:red">OFFLINE</div>
        
        <input id="u" placeholder="Bot Username">
        <input id="p" type="password" placeholder="Password">
        <input id="r" placeholder="Room Name">
        
        <button onclick="connect()" class="btn-on">CONNECT BOT</button>
        <button onclick="disconnect()" class="btn-off">DISCONNECT</button>
    </div>

    <div class="box">
        <div>SYSTEM LOGS:</div>
        <div id="logs"></div>
    </div>

    <script>
        function connect() {
            const u = document.getElementById('u').value, p = document.getElementById('p').value, r = document.getElementById('r').value;
            fetch('/connect', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({u, p, r})
            });
        }
        function disconnect() { fetch('/disconnect', {method: 'POST'}); }
        
        setInterval(() => {
            fetch('/logs').then(r=>r.json()).then(d => {
                document.getElementById('status').innerText = d.connected ? "ONLINE" : "OFFLINE";
                document.getElementById('status').style.color = d.connected ? "#00ff41" : "#ff003c";
                document.getElementById('logs').innerHTML = d.logs.map(l => 
                    `<div class="${l.type}">[${l.time}] ${l.msg}</div>`
                ).join('');
                const logDiv = document.getElementById('logs');
                logDiv.scrollTop = logDiv.scrollHeight;
            });
        }, 1000);
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
