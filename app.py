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
# 1. ASSETS (MEMORY)
# =============================================================================
ASSETS = {}

def init_assets():
    # 1. BOARD
    board = Image.new('RGB', (900, 900), (15, 15, 20)) 
    draw = ImageDraw.Draw(board)
    grid_color = (0, 243, 255) # Neon Cyan
    
    # Glow Border & Grid
    draw.rectangle([10, 10, 890, 890], outline=grid_color, width=15)
    for xy in [300, 600]:
        draw.line([(xy, 20), (xy, 880)], fill=grid_color, width=15)
        draw.line([(20, xy), (880, xy)], fill=grid_color, width=15)
    ASSETS['board'] = board

    # 2. X (Neon Red)
    x_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_x = ImageDraw.Draw(x_img)
    d_x.line([(60,60), (240,240)], fill=(255, 0, 60), width=25)
    d_x.line([(240,60), (60,240)], fill=(255, 0, 60), width=25)
    ASSETS['x'] = x_img

    # 3. O (Neon Green)
    o_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_o = ImageDraw.Draw(o_img)
    d_o.ellipse([60,60,240,240], outline=(0, 255, 65), width=25)
    ASSETS['o'] = o_img

init_assets()

# =============================================================================
# 2. DATABASE
# =============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_SQLITE = False if DATABASE_URL else True

def get_db():
    if USE_SQLITE: return sqlite3.connect("tictactoe.db")
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
# 3. CONNECTION & DEBUGGING
# =============================================================================

BOT = {
    "ws": None, 
    "status": "DISCONNECTED",
    "user": "", "pass": "", "room": "", 
    "should_run": False
}

# Two Types of Logs
LOGS = []         # For General Chat/Status
DEBUG_LOGS = []   # For Raw JSON Payloads

def log(msg, type="sys"):
    timestamp = time.strftime("%H:%M:%S")
    LOGS.append({"time": timestamp, "msg": msg, "type": type})
    if len(LOGS) > 50: LOGS.pop(0)

def log_debug(direction, payload):
    # Capture Raw JSON for the debug terminal
    timestamp = time.strftime("%H:%M:%S")
    try:
        # If string, convert to dict for pretty print, then back to string
        if isinstance(payload, str): payload = json.loads(payload)
    except: pass
    
    DEBUG_LOGS.append({"time": timestamp, "dir": direction, "data": payload})
    if len(DEBUG_LOGS) > 50: DEBUG_LOGS.pop(0)

def bot_thread():
    while BOT["should_run"]:
        try:
            BOT["status"] = "CONNECTING"
            log("Establishing Uplink...", "sys")
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
            log(f"Connection Failed: {e}", "err")
            log_debug("ERROR", str(e))
        
        if BOT["should_run"]:
            BOT["status"] = "RETRYING"
            time.sleep(5)

def on_open(ws):
    BOT["status"] = "AUTHENTICATING"
    login_pkt = {
        "handler": "login", "id": str(time.time()), 
        "username": BOT["user"], "password": BOT["pass"], "platform": "web"
    }
    ws.send(json.dumps(login_pkt))
    log_debug("OUT", login_pkt)
    
    def pinger():
        while BOT["ws"] and BOT["ws"].sock and BOT["ws"].sock.connected:
            time.sleep(20)
            try: ws.send(json.dumps({"handler": "ping"}))
            except: break
    threading.Thread(target=pinger, daemon=True).start()

def on_message(ws, message):
    try:
        # Capture Incoming Payload
        data = json.loads(message)
        
        # Don't clutter debug logs with ping/receipts unless necessary
        if data.get("handler") not in ["receipt_ack", "ping"]:
            log_debug("IN", data)

        if data.get("handler") == "login_event":
            if data["type"] == "success":
                BOT["status"] = "ONLINE"
                log(f"Access Granted. Joining {BOT['room']}...", "success")
                join_pkt = {"handler": "room_join", "id": str(time.time()), "name": BOT["room"]}
                ws.send(json.dumps(join_pkt))
                log_debug("OUT", join_pkt)
            else:
                log(f"Access Denied: {data.get('reason')}", "err")
                BOT["should_run"] = False
                ws.close()
                
        elif data.get("handler") == "room_event" and data.get("type") == "text":
            game_engine(data['from'], data['body'])
            
    except Exception as e: 
        log(f"Parse Error: {e}", "err")

def on_error(ws, error): 
    log(f"WS Error: {error}", "err")
    log_debug("ERROR", str(error))

def on_close(ws, c, m): 
    log("Uplink Terminated.", "sys")

def send_msg(text, type="text", url=""):
    if BOT["ws"]:
        pkt = {
            "handler": "room_message", "id": str(time.time()), 
            "room": BOT["room"], "type": type, "body": text, "url": url, "length": "0"
        }
        try:
            BOT["ws"].send(json.dumps(pkt))
            # Log the output
            debug_pkt = pkt.copy()
            if type == "image": debug_pkt["body"] = "[IMAGE DATA HIDDEN]"
            log_debug("OUT", debug_pkt)
        except: pass

# =============================================================================
# 4. GAME ENGINE
# =============================================================================
ACTIVE_GAMES = {}

def game_engine(user, msg):
    msg = msg.strip().lower()
    
    if msg == "!help":
        send_msg("🎮 **COMMANDS:**\n• `!start` (PvP)\n• `!start bot` (Solo)\n• `!join <user>`\n• `!stop`\n• `1-9` (Move)")
        return

    if msg.startswith("!start"):
        if find_user_game(user): return send_msg(f"⚠ {user}, busy! Type !stop.")
        mode = "bot" if "bot" in msg else "pvp"
        ACTIVE_GAMES[user] = {
            "host": user, "mode": mode, "board": [" "]*9, "turn": "X",
            "p1": user, "p2": "🤖 TitanBot" if mode=="bot" else None
        }
        send_board(user)
        if mode=="pvp": send_msg(f"🎮 **PvP LOBBY**\nHost: {user}\nType `!join {user}`")
        else: send_msg(f"🤖 **BOT MATCH**\n{user} vs TitanBot")
        return

    if msg.startswith("!join"):
        if find_user_game(user): return send_msg("⚠ Already playing.")
        parts = msg.split()
        if len(parts) < 2: return send_msg("⚠ Usage: `!join <host>`")
        host = parts[1]
        game = None
        for h in ACTIVE_GAMES:
            if h.lower() == host.lower(): game = ACTIVE_GAMES[h]; break
        if not game: return send_msg("⚠ Game not found.")
        if game["mode"] == "bot" or game["p2"]: return send_msg("⚠ Cannot join.")
        game["p2"] = user
        send_msg(f"⚔ **MATCH ON!**\n{user} (O) joined {game['p1']}.")
        return

    if msg == "!stop":
        game = find_user_game(user)
        if game:
            del ACTIVE_GAMES[game['host']]
            send_msg(f"🛑 Game hosted by {game['host']} stopped.")
        return

    if msg.isdigit():
        move = int(msg)
        game = find_user_game(user)
        if not game or move < 1 or move > 9: return
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

def find_user_game(u):
    for h, d in ACTIVE_GAMES.items():
        if d['p1']==u or d['p2']==u: return d
    return None

def run_bot(host):
    game = ACTIVE_GAMES.get(host)
    if not game: return
    b = game["board"]
    avail = [i for i,x in enumerate(b) if x == " "]
    if not avail: return
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
    if process_turn(game, "TitanBot"): return
    game["turn"] = "X"
    send_board(host)

def process_turn(game, mover):
    b = game["board"]
    win = None
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for x,y,z in wins:
        if b[x]==b[y]==b[z] and b[x]!=" ": win=f"{x},{y},{z}"; break
    host = game['host']
    if win:
        send_board(host, win)
        if "Bot" not in mover: add_win(mover)
        send_msg(f"🏆 **{mover} WINS!**")
        del ACTIVE_GAMES[host]
        return True
    elif " " not in b:
        send_board(host)
        send_msg("🤝 **DRAW!**")
        del ACTIVE_GAMES[host]
        return True
    return False

def send_board(host, line=""):
    game = ACTIVE_GAMES.get(host)
    if not game: return
    b_str = "".join(game["board"]).replace(" ", "_")
    url = f"{request.url_root}render?b={b_str}&w={line}&h={host}&t={int(time.time())}"
    send_msg("", "image", url)

# =============================================================================
# 5. ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(HTML_UI, u=BOT["user"], r=BOT["room"])

@app.route('/connect', methods=['POST'])
def connect():
    if BOT["should_run"]: return jsonify({"status": "Already Running"})
    d = request.json
    BOT.update({"user": d['u'], "pass": d['p'], "room": d['r'], "should_run": True})
    threading.Thread(target=bot_thread, daemon=True).start()
    return jsonify({"status": "Starting..."})

@app.route('/disconnect', methods=['POST'])
def disconnect():
    BOT["should_run"] = False
    if BOT["ws"]: BOT["ws"].close()
    return jsonify({"status": "Stopping..."})

@app.route('/logs')
def get_logs():
    # Return both general logs and debug logs
    return jsonify({
        "logs": LOGS, 
        "debug": DEBUG_LOGS,
        "status": BOT["status"]
    })

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
    except: return "Render Error", 500

# =============================================================================
# 6. HACKER INTERFACE (UI)
# =============================================================================
HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN // NETRUNNER</title>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --bg: #050505; --panel: rgba(20, 20, 20, 0.95); 
            --neon: #00f3ff; --term: #00ff41; --danger: #ff003c; --warn: #ffd700;
        }
        * { box-sizing: border-box; }
        body { margin: 0; background: var(--bg); color: var(--neon); font-family: 'Fira Code', monospace; height: 100vh; display: flex; flex-direction: column; overflow: hidden; background-image: radial-gradient(circle at 50% 50%, #111 0%, #000 100%); }
        
        /* SCROLLBARS */
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #000; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--neon); }

        /* HEADER */
        header { height: 60px; border-bottom: 2px solid var(--neon); display: flex; align-items: center; padding: 0 20px; justify-content: space-between; background: #0a0a0a; box-shadow: 0 0 15px rgba(0, 243, 255, 0.1); }
        .logo { font-size: 24px; font-weight: bold; letter-spacing: 2px; }
        .status-dot { height: 10px; width: 10px; border-radius: 50%; display: inline-block; margin-right: 5px; box-shadow: 0 0 5px currentColor; }
        
        /* MAIN LAYOUT */
        .layout { display: flex; flex: 1; overflow: hidden; }
        
        /* LEFT PANEL (Controls) */
        .controls { width: 300px; padding: 20px; border-right: 1px solid #333; display: flex; flex-direction: column; gap: 15px; background: rgba(10,10,10,0.8); }
        .input-group label { display: block; font-size: 11px; color: #666; margin-bottom: 4px; }
        input { width: 100%; background: #000; border: 1px solid #333; color: var(--term); padding: 10px; font-family: inherit; font-size: 14px; outline: none; transition: 0.2s; }
        input:focus { border-color: var(--neon); box-shadow: 0 0 8px rgba(0, 243, 255, 0.2); }
        
        button { width: 100%; padding: 12px; border: none; font-weight: bold; cursor: pointer; text-transform: uppercase; letter-spacing: 1px; transition: 0.2s; font-family: inherit; clip-path: polygon(10px 0, 100% 0, 100% calc(100% - 10px), calc(100% - 10px) 100%, 0 100%, 0 10px); }
        .btn-con { background: var(--term); color: #000; }
        .btn-con:hover { box-shadow: 0 0 15px var(--term); }
        .btn-dis { background: var(--danger); color: #fff; margin-top: auto; }
        .btn-dis:hover { box-shadow: 0 0 15px var(--danger); }

        /* RIGHT PANEL (Chat Logs) */
        .chat-panel { flex: 1; display: flex; flex-direction: column; border-right: 1px solid #333; }
        .panel-header { background: #111; padding: 5px 10px; font-size: 12px; color: #888; border-bottom: 1px solid #222; }
        #general-logs { flex: 1; padding: 15px; overflow-y: auto; font-size: 13px; line-height: 1.5; }
        
        /* BOTTOM PANEL (Debug Terminal) */
        .debug-container { height: 35%; border-top: 2px solid var(--neon); background: #080808; display: flex; flex-direction: column; }
        .debug-header { background: rgba(0, 243, 255, 0.1); padding: 5px 15px; font-size: 12px; color: var(--neon); display: flex; justify-content: space-between; font-weight: bold; }
        #debug-terminal { flex: 1; padding: 10px; overflow-y: auto; font-size: 12px; font-family: 'Fira Code', monospace; white-space: pre-wrap; color: #aaa; }
        
        /* LOG COLORS */
        .sys { color: #888; }
        .in { color: var(--term); }
        .out { color: var(--neon); }
        .err { color: var(--danger); }
        .json-key { color: var(--neon); }
        .json-string { color: var(--term); }
        .json-number { color: var(--warn); }
        .debug-entry { margin-bottom: 8px; border-left: 2px solid #333; padding-left: 10px; }
        .debug-in { border-color: var(--term); }
        .debug-out { border-color: var(--neon); }
        .debug-err { border-color: var(--danger); }

    </style>
</head>
<body>
    <header>
        <div class="logo">TITAN // OS</div>
        <div id="status-box" style="color:red"><span class="status-dot" style="background:red"></span>OFFLINE</div>
    </header>

    <div class="layout">
        <!-- CONTROLS -->
        <div class="controls">
            <div class="input-group">
                <label>BOT USERNAME</label>
                <input type="text" id="u" value="{{u}}" placeholder="TitanBot">
            </div>
            <div class="input-group">
                <label>PASSWORD</label>
                <input type="password" id="p" placeholder="••••••">
            </div>
            <div class="input-group">
                <label>ROOM NAME</label>
                <input type="text" id="r" value="{{r}}" placeholder="general">
            </div>
            <button onclick="connect()" class="btn-con">INITIALIZE</button>
            <button onclick="disconnect()" class="btn-dis">TERMINATE</button>
        </div>

        <!-- CHAT LOGS -->
        <div class="chat-panel">
            <div class="panel-header">SYSTEM EVENTS</div>
            <div id="general-logs"></div>
        </div>
    </div>

    <!-- DEBUG TERMINAL -->
    <div class="debug-container">
        <div class="debug-header">
            <span>DEBUG // STREAM</span>
            <span onclick="document.getElementById('debug-terminal').innerHTML=''" style="cursor:pointer; opacity:0.7">[CLEAR]</span>
        </div>
        <div id="debug-terminal">
            <div style="color:#555">Waiting for data stream...</div>
        </div>
    </div>

    <script>
        // --- SYNTAX HIGHLIGHTER FOR JSON ---
        function syntaxHighlight(json) {
            if (typeof json != 'string') {
                json = JSON.stringify(json, undefined, 2);
            }
            json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
                var cls = 'json-number';
                if (/^"/.test(match)) {
                    if (/:$/.test(match)) {
                        cls = 'json-key';
                    } else {
                        cls = 'json-string';
                    }
                } else if (/true|false/.test(match)) {
                    cls = 'json-boolean';
                } else if (/null/.test(match)) {
                    cls = 'json-null';
                }
                return '<span class="' + cls + '">' + match + '</span>';
            });
        }

        function connect() {
            const u = document.getElementById('u').value, p = document.getElementById('p').value, r = document.getElementById('r').value;
            fetch('/connect', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({u, p, r})
            });
        }
        function disconnect() { fetch('/disconnect', {method: 'POST'}); }

        // --- POLLING ---
        setInterval(() => {
            fetch('/logs').then(r=>r.json()).then(d => {
                // 1. Update Status
                const sBox = document.getElementById('status-box');
                const dot = sBox.querySelector('.status-dot');
                sBox.innerText = ""; 
                sBox.appendChild(dot);
                sBox.appendChild(document.createTextNode(d.status));
                
                let color = "red";
                if(d.status === "ONLINE") color = "#00ff41";
                else if(d.status === "CONNECTING") color = "#ffd700";
                
                sBox.style.color = color;
                dot.style.background = color;

                // 2. General Logs
                const logDiv = document.getElementById('general-logs');
                const logHtml = d.logs.map(l => 
                    `<div class="${l.type}"><b>[${l.time}]</b> ${l.msg}</div>`
                ).reverse().join('');
                if(logDiv.innerHTML !== logHtml) logDiv.innerHTML = logHtml;

                // 3. Debug Logs (Only append new ones in real implementation, here full refresh for simplicity)
                const debugDiv = document.getElementById('debug-terminal');
                const debugHtml = d.debug.map(l => {
                    let typeClass = "debug-out";
                    if(l.dir === "IN") typeClass = "debug-in";
                    if(l.dir === "ERROR") typeClass = "debug-err";
                    
                    return `<div class="debug-entry ${typeClass}">
                        <span style="opacity:0.6">[${l.time}] ${l.dir} >></span>
                        <pre style="margin:0; display:inline-block; vertical-align:top">${syntaxHighlight(l.data)}</pre>
                    </div>`;
                }).reverse().join('');
                
                // Only update if changed to avoid scroll jumping
                if(debugDiv.getAttribute('data-len') != d.debug.length) {
                    debugDiv.innerHTML = debugHtml;
                    debugDiv.setAttribute('data-len', d.debug.length);
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