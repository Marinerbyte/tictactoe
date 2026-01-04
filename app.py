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
# 1. ASSET GENERATION (IN-MEMORY)
# =============================================================================
ASSETS = {}

def init_assets():
    # 1. BOARD (HD Neon)
    board = Image.new('RGB', (900, 900), (18, 18, 24)) 
    draw = ImageDraw.Draw(board)
    grid_color = (0, 243, 255) # Cyan
    
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
# 2. DATABASE (Simple Score Keeper)
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
# 3. CONNECTION & DATA STATE
# =============================================================================

BOT = {
    "ws": None, 
    "status": "DISCONNECTED",
    "user": "", "pass": "", "room": "", "domain": "",
    "should_run": False,
    "avatars": {} # Cache for user avatars
}

# --- DATA STREAMS ---
CHAT_HISTORY = []    # Stores chat UI objects
DEBUG_PAYLOADS = []  # Stores Raw JSON for Debug Terminal

def save_chat(user, msg, avatar="", type="text"):
    timestamp = time.strftime("%H:%M")
    CHAT_HISTORY.append({
        "user": user, 
        "msg": msg, 
        "avatar": avatar, 
        "time": timestamp,
        "type": type
    })
    # Keep chat history manageable
    if len(CHAT_HISTORY) > 100: CHAT_HISTORY.pop(0)

def save_debug(direction, payload):
    timestamp = time.strftime("%H:%M:%S")
    try:
        if isinstance(payload, str): payload = json.loads(payload)
    except: pass
    
    DEBUG_PAYLOADS.append({
        "time": timestamp,
        "dir": direction,
        "data": payload
    })
    # Keep debug history (larger limit for analysis)
    if len(DEBUG_PAYLOADS) > 300: DEBUG_PAYLOADS.pop(0)

def bot_thread():
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
            save_debug("ERROR", str(e))
        
        if BOT["should_run"]:
            BOT["status"] = "RETRYING"
            time.sleep(5)

def on_open(ws):
    BOT["status"] = "AUTHENTICATING"
    pkt = {
        "handler": "login", "id": str(time.time()), 
        "username": BOT["user"], "password": BOT["pass"], "platform": "web"
    }
    ws.send(json.dumps(pkt))
    save_debug("OUT", pkt)
    
    def pinger():
        while BOT["ws"] and BOT["ws"].sock and BOT["ws"].sock.connected:
            time.sleep(20)
            try: ws.send(json.dumps({"handler": "ping"}))
            except: break
    threading.Thread(target=pinger, daemon=True).start()

def on_message(ws, message):
    try:
        data = json.loads(message)
        
        # Capture Avatar if present
        if data.get("avatar_url") and data.get("from"):
            BOT["avatars"][data["from"]] = data["avatar_url"]

        # Only log non-heartbeat packets to debug
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
            # Add to Chat UI
            av = BOT["avatars"].get(data['from'], "https://cdn-icons-png.flaticon.com/512/149/149071.png")
            save_chat(data['from'], data['body'], av, "text")
            
            # Process Game
            game_engine(data['from'], data['body'])
            
    except Exception as e: save_debug("ERROR", str(e))

def on_error(ws, error): save_debug("WS ERROR", str(error))
def on_close(ws, c, m): BOT["status"] = "DISCONNECTED"

def send_msg(text, type="text", url=""):
    if BOT["ws"]:
        pkt = {
            "handler": "room_message", "id": str(time.time()), 
            "room": BOT["room"], "type": type, "body": text, "url": url, "length": "0"
        }
        try:
            BOT["ws"].send(json.dumps(pkt))
            
            # Add Bot's message to UI
            bot_av = "https://cdn-icons-png.flaticon.com/512/4712/4712035.png"
            msg_content = text if type == "text" else "[IMAGE SENT]"
            save_chat("TitanBot", msg_content, bot_av, "bot")
            save_debug("OUT", pkt)
        except: pass

# =============================================================================
# 4. GAME ENGINE (Multi-Instance)
# =============================================================================
ACTIVE_GAMES = {}

def game_engine(user, msg):
    msg = msg.strip().lower()
    
    if msg == "!help":
        send_msg("🎮 **COMMANDS:**\n• `!start` (PvP)\n• `!start bot` (Solo)\n• `!join <user>`\n• `!stop`\n• `1-9` (Move)")
        return

    if msg.startswith("!start"):
        if find_user_game(user): return send_msg(f"⚠ {user}, type !stop first.")
        mode = "bot" if "bot" in msg else "pvp"
        ACTIVE_GAMES[user] = {
            "host": user, "mode": mode, "board": [" "]*9, "turn": "X",
            "p1": user, "p2": "🤖 TitanBot" if mode=="bot" else None
        }
        send_board(user)
        if mode=="pvp": send_msg(f"🎮 **PvP LOBBY**\nHost: {user}\nWaiting: `!join {user}`")
        else: send_msg(f"🤖 **BOT MATCH**\n{user} vs TitanBot")
        return

    if msg.startswith("!join"):
        if find_user_game(user): return send_msg("⚠ Already playing.")
        parts = msg.split()
        if len(parts) < 2: return send_msg("Usage: `!join <host>`")
        host = parts[1]
        game = None
        for h in ACTIVE_GAMES:
            if h.lower() == host.lower(): game = ACTIVE_GAMES[h]; break
        if not game: return send_msg("⚠ Game not found.")
        if game["mode"] == "bot" or game["p2"]: return send_msg("⚠ Cannot join.")
        game["p2"] = user
        send_msg(f"⚔ **MATCH ON!**\n{user} joined {game['p1']}.")
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
    base_url = BOT.get('domain', '') 
    if not base_url: return
    url = f"{base_url}render?b={b_str}&w={line}&h={host}&t={int(time.time())}"
    send_msg("", "image", url)

# =============================================================================
# 5. FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(UI_TEMPLATE)

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
    return jsonify({"status": "Stopping..."})

@app.route('/get_data')
def get_data():
    return jsonify({
        "status": BOT["status"],
        "chat": CHAT_HISTORY,
        "debug": DEBUG_PAYLOADS
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
    except: return "Error", 500

# =============================================================================
# 6. PROFESSIONAL UI TEMPLATE
# =============================================================================
UI_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN OS // PRO</title>
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0b0c15;
            --panel: rgba(23, 25, 35, 0.85);
            --primary: #00f3ff;
            --accent: #ff003c;
            --text: #e0e6ed;
            --chat-bg: #12141c;
        }
        * { box-sizing: border-box; }
        body { margin: 0; background: var(--bg); color: var(--text); font-family: 'Rajdhani', sans-serif; height: 100vh; overflow: hidden; }
        
        /* LOGIN SCREEN */
        #login-view {
            position: absolute; top:0; left:0; width:100%; height:100%;
            display: flex; justify-content: center; align-items: center;
            background: radial-gradient(circle at center, #1a1d29 0%, #000 100%);
            z-index: 10; transition: 0.5s opacity;
        }
        .login-card {
            background: var(--panel); padding: 40px; border-radius: 12px;
            width: 350px; border: 1px solid rgba(255,255,255,0.1);
            box-shadow: 0 0 30px rgba(0, 243, 255, 0.1);
            backdrop-filter: blur(10px); text-align: center;
        }
        .logo { font-size: 32px; font-weight: bold; color: var(--primary); margin-bottom: 20px; letter-spacing: 2px; }
        input {
            width: 100%; background: rgba(0,0,0,0.3); border: 1px solid #333;
            padding: 12px; margin-bottom: 15px; color: #fff; border-radius: 6px;
            font-family: 'Roboto Mono', monospace; outline: none; transition: 0.3s;
        }
        input:focus { border-color: var(--primary); }
        .btn-login {
            width: 100%; padding: 12px; background: var(--primary); color: #000;
            border: none; border-radius: 6px; font-weight: bold; cursor: pointer;
            font-size: 16px; transition: 0.3s;
        }
        .btn-login:hover { box-shadow: 0 0 15px var(--primary); transform: translateY(-2px); }

        /* MAIN APP VIEW */
        #app-view {
            display: none; height: 100%; flex-direction: column;
            background: url('https://i.imgur.com/8Q5k1kE.png'); /* Subtle pattern */
        }
        
        /* HEADER */
        header {
            height: 60px; background: rgba(10,12,18,0.9); border-bottom: 1px solid #333;
            display: flex; align-items: center; justify-content: space-between; padding: 0 20px;
        }
        .status-badge {
            padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: bold;
            background: rgba(255,0,0,0.2); color: var(--accent); border: 1px solid var(--accent);
        }
        .status-online { background: rgba(0,255,65,0.2); color: #00ff41; border-color: #00ff41; }
        
        .btn-logout {
            padding: 6px 15px; background: transparent; border: 1px solid #444; color: #aaa;
            border-radius: 4px; cursor: pointer; font-size: 12px;
        }
        .btn-logout:hover { border-color: var(--accent); color: var(--accent); }

        /* CONTENT AREA */
        .content { flex: 1; position: relative; overflow: hidden; display: flex; }
        
        /* TABS */
        .tab-content {
            position: absolute; top:0; left:0; width:100%; height:100%;
            display: none; flex-direction: column;
        }
        .active-tab { display: flex; }

        /* CHAT UI */
        #chat-container {
            flex: 1; padding: 20px; overflow-y: auto; background: var(--chat-bg);
            display: flex; flex-direction: column; gap: 15px;
        }
        .msg-row { display: flex; gap: 12px; max-width: 80%; animation: fadeIn 0.3s; }
        .msg-left { align-self: flex-start; }
        .msg-right { align-self: flex-end; flex-direction: row-reverse; }
        
        .avatar { width: 40px; height: 40px; border-radius: 50%; border: 2px solid #333; object-fit: cover; }
        .bubble {
            background: #232634; padding: 10px 15px; border-radius: 12px;
            border-top-left-radius: 2px; position: relative; font-size: 14px; line-height: 1.4;
        }
        .msg-right .bubble { background: #005f63; border-top-left-radius: 12px; border-top-right-radius: 2px; }
        .sender-name { font-size: 11px; color: var(--primary); margin-bottom: 2px; display: block; }
        .timestamp { font-size: 10px; color: #666; display: block; text-align: right; margin-top: 5px; }

        /* DEBUG UI */
        #debug-container {
            flex: 1; background: #080808; color: #bbb; padding: 0;
            display: flex; flex-direction: column; font-family: 'Roboto Mono', monospace;
        }
        .debug-toolbar {
            padding: 10px; border-bottom: 1px solid #333; display: flex; justify-content: flex-end; gap: 10px;
        }
        .debug-log-area {
            flex: 1; padding: 15px; overflow-y: auto; font-size: 12px;
        }
        .log-entry { margin-bottom: 8px; border-left: 2px solid #333; padding-left: 10px; }
        .dir-IN { border-color: #00ff41; }
        .dir-OUT { border-color: var(--primary); }
        .dir-ERROR { border-color: var(--accent); }
        
        .json-key { color: var(--primary); }
        .json-string { color: #ce9178; }
        .json-number { color: #b5cea8; }

        /* BOTTOM NAV */
        .bottom-nav {
            height: 50px; background: #1a1d29; border-top: 1px solid #333;
            display: flex;
        }
        .nav-btn {
            flex: 1; background: transparent; border: none; color: #666;
            font-size: 14px; font-weight: bold; cursor: pointer;
            border-bottom: 3px solid transparent; transition: 0.2s;
        }
        .nav-btn.active { color: #fff; border-bottom-color: var(--primary); background: rgba(0, 243, 255, 0.05); }

        /* UTILS */
        .btn-sm { padding: 5px 10px; background: #333; color: #fff; border:none; border-radius: 4px; cursor: pointer; }
        .btn-sm:hover { background: #555; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
    </style>
</head>
<body>

    <!-- LOGIN SCREEN -->
    <div id="login-view">
        <div class="login-card">
            <div class="logo">TITAN // OS</div>
            <input type="text" id="u" placeholder="Bot Username">
            <input type="password" id="p" placeholder="Password">
            <input type="text" id="r" placeholder="Room Name">
            <button onclick="login()" class="btn-login">INITIALIZE LINK</button>
        </div>
    </div>

    <!-- MAIN APP SCREEN -->
    <div id="app-view">
        <header>
            <div style="font-weight:bold; letter-spacing:1px;">TITAN CONTROL PANEL</div>
            <div id="status-badge" class="status-badge">OFFLINE</div>
            <button onclick="logout()" class="btn-logout">DISCONNECT</button>
        </header>

        <div class="content">
            <!-- TAB 1: CHAT -->
            <div id="tab-chat" class="tab-content active-tab">
                <div id="chat-container"></div>
            </div>

            <!-- TAB 2: DEBUG -->
            <div id="tab-debug" class="tab-content">
                <div class="debug-toolbar">
                    <button onclick="clearDebug()" class="btn-sm">CLEAR</button>
                    <button onclick="downloadLogs()" class="btn-sm" style="color:var(--primary)">⬇ DOWNLOAD LOGS</button>
                </div>
                <div id="debug-log-area" class="debug-log-area"></div>
            </div>
        </div>

        <nav class="bottom-nav">
            <button onclick="switchTab('chat')" id="btn-chat" class="nav-btn active">LIVE CHAT</button>
            <button onclick="switchTab('debug')" id="btn-debug" class="nav-btn">DEBUG TERMINAL</button>
        </nav>
    </div>

    <script>
        let currentTab = 'chat';
        let debugPayloads = [];

        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active-tab'));
            document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('active'));
            
            document.getElementById('tab-' + tab).classList.add('active-tab');
            document.getElementById('btn-' + tab).classList.add('active');
            
            // Auto scroll on switch
            if(tab === 'chat') scrollToBottom('chat-container');
            if(tab === 'debug') scrollToBottom('debug-log-area');
        }

        function login() {
            const u = document.getElementById('u').value;
            const p = document.getElementById('p').value;
            const r = document.getElementById('r').value;
            if(!u || !p || !r) return alert("Enter all details");

            document.getElementById('login-view').style.opacity = '0';
            setTimeout(() => {
                document.getElementById('login-view').style.display = 'none';
                document.getElementById('app-view').style.display = 'flex';
            }, 500);

            fetch('/connect', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({u, p, r})
            });
        }

        function logout() {
            if(confirm("Disconnect Bot?")) {
                fetch('/disconnect', {method: 'POST'});
                location.reload();
            }
        }

        function scrollToBottom(id) {
            const el = document.getElementById(id);
            el.scrollTop = el.scrollHeight;
        }

        function syntaxHighlight(json) {
            if (typeof json != 'string') json = JSON.stringify(json, undefined, 2);
            json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function (match) {
                var cls = 'json-number';
                if (/^"/.test(match)) {
                    if (/:$/.test(match)) cls = 'json-key';
                    else cls = 'json-string';
                }
                return '<span class="' + cls + '">' + match + '</span>';
            });
        }

        function downloadLogs() {
            const text = debugPayloads.map(p => 
                `[${p.time}] ${p.dir} >>\n${JSON.stringify(p.data, null, 2)}\n-----------------------------------\n`
            ).join('');
            const blob = new Blob([text], { type: 'text/plain' });
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `titan_debug_${Date.now()}.txt`;
            a.click();
        }
        
        function clearDebug() {
            document.getElementById('debug-log-area').innerHTML = '';
        }

        // POLLING LOOP
        setInterval(() => {
            fetch('/get_data').then(r=>r.json()).then(d => {
                // UPDATE STATUS
                const badge = document.getElementById('status-badge');
                badge.innerText = d.status;
                if(d.status === 'ONLINE') badge.className = 'status-badge status-online';
                else badge.className = 'status-badge';

                // UPDATE CHAT
                const chatDiv = document.getElementById('chat-container');
                // Only redraw if length changed to avoid jitter, ideally append
                // Simple logic: regenerate all
                const chatHtml = d.chat.map(m => {
                    const side = m.type === 'bot' ? 'msg-right' : 'msg-left';
                    return `
                    <div class="msg-row ${side}">
                        <img src="${m.avatar}" class="avatar" onerror="this.src='https://cdn-icons-png.flaticon.com/512/149/149071.png'">
                        <div class="bubble">
                            <span class="sender-name">${m.user}</span>
                            ${m.msg}
                            <span class="timestamp">${m.time}</span>
                        </div>
                    </div>`;
                }).join('');
                
                if(chatDiv.innerHTML.length !== chatHtml.length) {
                    chatDiv.innerHTML = chatHtml;
                    if(currentTab === 'chat') scrollToBottom('chat-container');
                }

                // UPDATE DEBUG
                debugPayloads = d.debug; // Save for download
                const debugDiv = document.getElementById('debug-log-area');
                const debugHtml = d.debug.map(l => `
                    <div class="log-entry dir-${l.dir}">
                        <div style="opacity:0.7">[${l.time}] ${l.dir} >></div>
                        <pre style="margin:0">${syntaxHighlight(l.data)}</pre>
                    </div>
                `).reverse().join(''); // Show newest at top if you prefer, or bottom
                
                // For terminal feel, usually newest at bottom. Let's reverse data order logic.
                // Current backend sends [oldest ... newest]. 
                // We want to render them in order.
                const debugHtmlCorrect = d.debug.map(l => `
                    <div class="log-entry dir-${l.dir}">
                        <div style="opacity:0.7">[${l.time}] ${l.dir} >></div>
                        <pre style="margin:0">${syntaxHighlight(l.data)}</pre>
                    </div>
                `).join('');

                if(debugDiv.getAttribute('data-len') != d.debug.length) {
                    debugDiv.innerHTML = debugHtmlCorrect;
                    debugDiv.setAttribute('data-len', d.debug.length);
                    if(currentTab === 'debug') scrollToBottom('debug-log-area');
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