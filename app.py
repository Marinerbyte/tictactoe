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
# 1. ASSET GENERATION (UNCHANGED)
# =============================================================================
ASSETS = {}

def init_assets():
    # 1. BOARD
    board = Image.new('RGB', (900, 900), (18, 18, 24)) 
    draw = ImageDraw.Draw(board)
    grid_color = (0, 243, 255)
    
    draw.rectangle([10, 10, 890, 890], outline=grid_color, width=15)
    for xy in [300, 600]:
        draw.line([(xy, 20), (xy, 880)], fill=grid_color, width=15)
        draw.line([(20, xy), (880, xy)], fill=grid_color, width=15)
    ASSETS['board'] = board

    # 2. X
    x_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_x = ImageDraw.Draw(x_img)
    d_x.line([(60,60), (240,240)], fill=(255, 0, 60), width=25)
    d_x.line([(240,60), (60,240)], fill=(255, 0, 60), width=25)
    ASSETS['x'] = x_img

    # 3. O
    o_img = Image.new('RGBA', (300, 300), (0,0,0,0))
    d_o = ImageDraw.Draw(o_img)
    d_o.ellipse([60,60,240,240], outline=(0, 255, 65), width=25)
    ASSETS['o'] = o_img

init_assets()

# =============================================================================
# 2. DATABASE (UNCHANGED)
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
# 3. CONNECTION & DATA STATE (UNCHANGED)
# =============================================================================

BOT = {
    "ws": None, "status": "DISCONNECTED",
    "user": "", "pass": "", "room": "", "domain": "",
    "should_run": False, "avatars": {}
}

CHAT_HISTORY = []    
DEBUG_PAYLOADS = [] 

def save_chat(user, msg, avatar="", type="text"):
    timestamp = time.strftime("%H:%M")
    CHAT_HISTORY.append({"user": user, "msg": msg, "avatar": avatar, "time": timestamp, "type": type})
    if len(CHAT_HISTORY) > 100: CHAT_HISTORY.pop(0)

def save_debug(direction, payload):
    timestamp = time.strftime("%H:%M:%S")
    try:
        if isinstance(payload, str): payload = json.loads(payload)
    except: pass
    DEBUG_PAYLOADS.append({"time": timestamp, "dir": direction, "data": payload})
    if len(DEBUG_PAYLOADS) > 300: DEBUG_PAYLOADS.pop(0)

def bot_thread():
    while BOT["should_run"]:
        try:
            BOT["status"] = "CONNECTING"
            ws = websocket.WebSocketApp("wss://chatp.net:5333/server",
                on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            BOT["ws"] = ws
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e: save_debug("ERROR", str(e))
        if BOT["should_run"]:
            BOT["status"] = "RETRYING"
            time.sleep(5)

def on_open(ws):
    BOT["status"] = "AUTHENTICATING"
    pkt = {"handler": "login", "id": str(time.time()), "username": BOT["user"], "password": BOT["pass"], "platform": "web"}
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
        if data.get("avatar_url") and data.get("from"): BOT["avatars"][data["from"]] = data["avatar_url"]
        if data.get("handler") not in ["receipt_ack", "ping"]: save_debug("IN", data)

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
            msg_content = text if type == "text" else "[IMAGE SENT]"
            save_chat("TitanBot", msg_content, bot_av, "bot")
            save_debug("OUT", pkt)
        except: pass

# =============================================================================
# 4. GAME ENGINE (UNCHANGED)
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
# 5. ROUTES
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
# 6. MOBILE-OPTIMIZED UI
# =============================================================================
UI_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>TITAN OS // MOBILE</title>
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #050505;
            --panel: rgba(23, 25, 35, 0.95);
            --primary: #00f3ff;
            --accent: #ff003c;
            --text: #e0e6ed;
            --chat-bg: #000000;
            /* SAFE AREA FOR MOBILE BOTTOM NAV */
            --safe-bottom: env(safe-area-inset-bottom, 20px);
        }
        * { box-sizing: border-box; }
        
        body { 
            margin: 0; 
            background: var(--bg); 
            color: var(--text); 
            font-family: 'Rajdhani', sans-serif; 
            height: 100dvh; /* Dynamic viewport height fix */
            width: 100vw;
            overflow: hidden; 
            display: flex;
            flex-direction: column;
        }

        /* BACKGROUND GRID EFFECT (NO IMGUR) */
        body::before {
            content: "";
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            background-image: 
                linear-gradient(rgba(0, 243, 255, 0.05) 1px, transparent 1px),
                linear-gradient(90deg, rgba(0, 243, 255, 0.05) 1px, transparent 1px);
            background-size: 30px 30px;
            z-index: -1;
        }

        /* LOGIN OVERLAY */
        #login-view {
            position: absolute; top:0; left:0; width:100%; height:100%;
            display: flex; justify-content: center; align-items: center;
            background: #000; z-index: 999; transition: 0.5s opacity;
        }
        .login-card {
            width: 85%; max-width: 350px;
            padding: 30px; background: #111; 
            border: 1px solid #333; border-radius: 12px;
            text-align: center;
            box-shadow: 0 0 20px rgba(0,243,255,0.1);
        }
        .logo { font-size: 28px; color: var(--primary); margin-bottom: 20px; font-weight:bold; letter-spacing:2px; }
        input {
            width: 100%; padding: 12px; margin-bottom: 12px;
            background: #000; border: 1px solid #333; color: #fff;
            border-radius: 6px; font-family: 'JetBrains Mono', monospace;
        }
        input:focus { border-color: var(--primary); outline: none; }
        .btn-login {
            width: 100%; padding: 14px; background: var(--primary);
            color: #000; font-weight: bold; border: none; border-radius: 6px;
            font-size: 16px; margin-top: 10px;
        }

        /* APP LAYOUT */
        #app-view {
            display: none; height: 100%; flex-direction: column;
            width: 100%;
        }

        /* HEADER */
        header {
            height: 60px; flex-shrink: 0;
            background: rgba(10,12,18,0.95); border-bottom: 1px solid #333;
            display: flex; align-items: center; justify-content: space-between;
            padding: 0 15px;
        }
        .title { font-weight: bold; font-size: 18px; letter-spacing: 1px; color: #fff; }
        
        .header-actions { display: flex; gap: 10px; align-items: center; }
        
        .status-badge {
            font-size: 10px; padding: 4px 8px; border-radius: 4px;
            background: #222; color: #666; font-weight: bold; border: 1px solid #444;
        }
        .status-online { background: rgba(0,255,0,0.1); color: #0f0; border-color: #0f0; }

        .btn-logout {
            background: transparent; border: 1px solid #ff003c; color: #ff003c;
            padding: 4px 10px; border-radius: 4px; font-size: 10px; font-weight: bold;
        }

        /* MAIN CONTENT AREA */
        .content {
            flex: 1; /* Fills remaining space */
            position: relative;
            overflow: hidden; 
            background: rgba(0,0,0,0.5);
        }
        
        /* TABS */
        .tab-content {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            display: none; flex-direction: column;
        }
        .active-tab { display: flex; }

        /* CHAT STYLE */
        #chat-container {
            flex: 1; overflow-y: auto; padding: 15px;
            display: flex; flex-direction: column; gap: 12px;
            padding-bottom: 20px;
        }
        .msg-row { display: flex; gap: 10px; max-width: 85%; }
        .msg-left { align-self: flex-start; }
        .msg-right { align-self: flex-end; flex-direction: row-reverse; }
        
        .avatar { width: 35px; height: 35px; border-radius: 50%; border: 1px solid #333; flex-shrink: 0; background: #222; }
        .bubble {
            background: #1e1e1e; padding: 10px; border-radius: 12px;
            border-top-left-radius: 2px; font-size: 14px; line-height: 1.4; color: #eee;
        }
        .msg-right .bubble { background: #004d40; border-top-left-radius: 12px; border-top-right-radius: 2px; }
        .sender-name { font-size: 10px; color: var(--primary); display: block; margin-bottom: 2px; }

        /* DEBUG STYLE */
        #debug-container {
            flex: 1; display: flex; flex-direction: column;
            background: #0a0a0a; border-left: 1px solid #222;
        }
        .debug-toolbar {
            padding: 10px; border-bottom: 1px solid #333; background: #111;
            display: flex; justify-content: space-between;
        }
        .debug-logs {
            flex: 1; overflow-y: auto; padding: 10px;
            font-family: 'JetBrains Mono', monospace; font-size: 11px;
        }
        .log-entry { margin-bottom: 10px; border-bottom: 1px solid #222; padding-bottom: 5px; }
        .dir-IN { color: #0f0; }
        .dir-OUT { color: #0ff; }
        .dir-ERROR { color: #f00; }
        pre { margin: 0; white-space: pre-wrap; word-break: break-all; }

        /* BOTTOM NAV */
        nav {
            height: 60px; flex-shrink: 0;
            background: #000; border-top: 1px solid #333;
            display: flex;
            padding-bottom: var(--safe-bottom); /* Fix for iPhone home bar */
        }
        .nav-btn {
            flex: 1; background: transparent; border: none;
            color: #555; font-size: 14px; font-weight: bold;
            display: flex; align-items: center; justify-content: center;
            border-top: 3px solid transparent;
        }
        .nav-btn.active { color: #fff; border-top-color: var(--primary); background: #111; }

        /* SCROLLBAR */
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 2px; }
    </style>
</head>
<body>

    <!-- LOGIN -->
    <div id="login-view">
        <div class="login-card">
            <div class="logo">TITAN MOBILE</div>
            <input type="text" id="u" placeholder="Username">
            <input type="password" id="p" placeholder="Password">
            <input type="text" id="r" placeholder="Room Name">
            <button onclick="login()" class="btn-login">CONNECT</button>
        </div>
    </div>

    <!-- APP -->
    <div id="app-view">
        <header>
            <div class="title">TITAN PANEL</div>
            <div class="header-actions">
                <div id="status-badge" class="status-badge">OFFLINE</div>
                <button onclick="logout()" class="btn-logout">EXIT</button>
            </div>
        </header>

        <div class="content">
            <!-- CHAT TAB -->
            <div id="tab-chat" class="tab-content active-tab">
                <div id="chat-container"></div>
            </div>

            <!-- DEBUG TAB -->
            <div id="tab-debug" class="tab-content">
                <div class="debug-toolbar">
                    <span style="color:#666; font-size:12px">LIVE LOGS</span>
                    <div>
                        <button onclick="clearDebug()" style="background:#333;border:none;color:#fff;padding:5px;border-radius:4px;font-size:10px">CLEAR</button>
                        <button onclick="downloadLogs()" style="background:var(--primary);border:none;color:#000;padding:5px;border-radius:4px;font-size:10px;font-weight:bold">⬇ SAVE</button>
                    </div>
                </div>
                <div id="debug-log-area" class="debug-logs"></div>
            </div>
        </div>

        <nav>
            <button onclick="switchTab('chat')" id="btn-chat" class="nav-btn active">CHAT ROOM</button>
            <button onclick="switchTab('debug')" id="btn-debug" class="nav-btn">DEBUGGER</button>
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
            
            if(tab === 'chat') scrollToBottom('chat-container');
            if(tab === 'debug') scrollToBottom('debug-log-area');
        }

        function login() {
            const u = document.getElementById('u').value;
            const p = document.getElementById('p').value;
            const r = document.getElementById('r').value;
            if(!u || !p || !r) return alert("Fill all fields");

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
            if(confirm("Stop Bot?")) {
                fetch('/disconnect', {method: 'POST'});
                location.reload();
            }
        }

        function scrollToBottom(id) {
            const el = document.getElementById(id);
            el.scrollTop = el.scrollHeight;
        }

        function downloadLogs() {
            const text = debugPayloads.map(p => 
                `[${p.time}] ${p.dir} >> ${JSON.stringify(p.data)}`
            ).join('\\n');
            const blob = new Blob([text], { type: 'text/plain' });
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `titan_debug.txt`;
            a.click();
        }
        
        function clearDebug() {
            document.getElementById('debug-log-area').innerHTML = '';
        }

        setInterval(() => {
            fetch('/get_data').then(r=>r.json()).then(d => {
                // STATUS
                const badge = document.getElementById('status-badge');
                badge.innerText = d.status;
                if(d.status === 'ONLINE') badge.className = 'status-badge status-online';
                else badge.className = 'status-badge';

                // CHAT
                const chatDiv = document.getElementById('chat-container');
                const chatHtml = d.chat.map(m => {
                    const side = m.type === 'bot' ? 'msg-right' : 'msg-left';
                    return `
                    <div class="msg-row ${side}">
                        <img src="${m.avatar}" class="avatar" onerror="this.src='https://cdn-icons-png.flaticon.com/512/149/149071.png'">
                        <div class="bubble">
                            <span class="sender-name">${m.user}</span>
                            ${m.msg}
                        </div>
                    </div>`;
                }).join('');
                
                if(chatDiv.innerHTML.length !== chatHtml.length) {
                    chatDiv.innerHTML = chatHtml;
                    if(currentTab === 'chat') scrollToBottom('chat-container');
                }

                // DEBUG
                debugPayloads = d.debug;
                const debugDiv = document.getElementById('debug-log-area');
                // Only render last 50 to keep mobile fast
                const visibleDebug = d.debug.slice(-50);
                
                const debugHtml = visibleDebug.map(l => `
                    <div class="log-entry">
                        <span style="opacity:0.5">[${l.time}]</span> 
                        <span class="dir-${l.dir}">${l.dir}</span>
                        <pre style="color:#bbb">${JSON.stringify(l.data, null, 2)}</pre>
                    </div>
                `).join('');

                if(debugDiv.getAttribute('data-len') != d.debug.length) {
                    debugDiv.innerHTML = debugHtml;
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