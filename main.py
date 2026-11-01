import os, time, threading, yfinance as yf, pandas as pd, requests, ta, json
from flask import Flask
from datetime import datetime
import pytz
app = Flask(__name__)

CONFIG = {"SYMBOL": "GC=F", "SIGNAL_TF": "15m", "TREND_TF": "1h", "SWING_BARS": 2, "RSI_LENGTH": 14, "RSI_LOWER": 25, "RSI_UPPER": 75, "PAPER_MODE": True}
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

active_trades = []
last_bar = None
STATS_FILE = "paper_stats.json"

def load_stats():
    try:
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    except:
        return {"trades": [], "week_start": str(datetime.now().date())}

def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)

def is_market_open():
    paris = pytz.timezone('Europe/Paris')
    now = datetime.now(paris)
    day, h = now.weekday(), now.hour
    # Fermeture Forex réelle: Ven 23h -> Dim 23h + Pause quotidienne 23h-00h
    if day == 4 and h >= 23: return False
    if day == 5: return False
    if day == 6 and h < 23: return False
    if h == 23: return False
    return True

def get_df(tf):
    period = "10d" if tf=="15m" else "1mo"
    try:
        df = yf.download(CONFIG["SYMBOL"], period=period, interval=tf, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if len(df) < 50: return None
        df['rsi'] = ta.momentum.RSIIndicator(df['Close'], window=CONFIG["RSI_LENGTH"]).rsi()
        return df.dropna()
    except: return None

def send_tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def check_new_signal():
    global last_bar
    df15 = get_df(CONFIG["SIGNAL_TF"])
    df1h = get_df(CONFIG["TREND_TF"])
    if df15 is None or df1h is None or len(df15) < 100: return None
    i = len(df15) - 2
    if i == last_bar: return None
    cnt = CONFIG["SWING_BARS"]
    window = df15.iloc[i - 2*cnt : i+1]
    center = i - cnt
    is_high = all(df15['High'].iloc[j] <= df15['High'].iloc[center] for j in range(i-2*cnt, i+1) if j!=center)
    is_low = all(df15['Low'].iloc[j] >= df15['Low'].iloc[center] for j in range(i-2*cnt, i+1) if j!=center)
    rsi_high = (window['rsi'] > CONFIG["RSI_UPPER"]).any()
    rsi_low = (window['rsi'] < CONFIG["RSI_LOWER"]).any()
    if is_low and is_high: return None
    color_w = df15.iloc[center : i+1]
    bullish = all(color_w['Close'] > color_w['Open'])
    bearish = all(color_w['Close'] < color_w['Open'])
    orig = 1 if (is_low and rsi_low) else -1 if (is_high and rsi_high) else 0
    signal = 1 if orig==1 and not bullish else -1 if orig==-1 and not bearish else 0
    if signal == 0: return None
    rsi_1h = df1h['rsi'].iloc[-2]
    if signal == 1 and rsi_1h < 50: return None
    if signal == -1 and rsi_1h > 50: return None
    entry = float(df15['Close'].iloc[i])
    stop = float(df15['Low'].iloc[i-8] if signal==1 else df15['High'].iloc[i-8])
    take = float(df15['High'].iloc[center] if signal==1 else df15['Low'].iloc[center])
    if signal*(entry-stop) <=0: return None
    last_bar = i
    risk = abs(entry-stop)
    tp1 = entry + risk*1.8 if signal==1 else entry - risk*1.8
    return {"side": "LONG" if signal==1 else "SHORT", "entry": entry, "stop": stop, "tp1": tp1, "tp2": take, "be_done": False, "tp1_done": False}

def manage_trades():
    if not active_trades: return
    try:
        price = float(yf.Ticker(CONFIG["SYMBOL"]).fast_info['last_price'])
    except:
        df = get_df("15m")
        if df is None: return
        price = float(df['Close'].iloc[-1])
    stats = load_stats()
    for t in active_trades[:]:
        side = t['side']
        if not t['tp1_done']:
            hit_tp1 = price >= t['tp1'] if side=="LONG" else price <= t['tp1']
            if hit_tp1:
                t['tp1_done'] = True
                t['be_done'] = True
                send_tg(f"✅ **TP1 TOUCHÉ PAPER** {side}\nPrix `{price:.2f}` -> Passe en BE à `{t['entry']:.2f}`")
        hit_tp2 = price >= t['tp2'] if side=="LONG" else price <= t['tp2']
        sl_price = t['entry'] if t['be_done'] else t['stop']
        hit_sl = price <= sl_price if side=="LONG" else price >= sl_price
        if hit_tp2:
            r = abs(t['tp2']-t['entry'])/abs(t['entry']-t['stop'])
            stats["trades"].append({"result": "TP2", "R": r, "side": side})
            save_stats(stats)
            send_tg(f"🎯 **TP2 TOUCHÉ PAPER** {side} à {price:.2f} -> +{r:.1f}R")
            active_trades.remove(t)
        elif hit_sl and t['tp1_done']:
            stats["trades"].append({"result": "BE", "R": 0.8, "side": side})
            save_stats(stats)
            send_tg(f"🔒 **BE TOUCHÉ PAPER** {side} -> +0.8R sécurisé")
            active_trades.remove(t)
        elif hit_sl and not t['tp1_done']:
            stats["trades"].append({"result": "SL", "R": -1, "side": side})
            save_stats(stats)
            send_tg(f"❌ **SL TOUCHÉ PAPER** {side} à {price:.2f} -> -1R")
            active_trades.remove(t)

def weekly_report():
    paris = pytz.timezone('Europe/Paris')
    while True:
        now = datetime.now(paris)
        if now.weekday() == 6 and now.hour == 22 and now.minute == 55:
            stats = load_stats()
            trades = stats["trades"]
            if not trades:
                send_tg("📊 **BILAN HEBDO**\nAucun trade cette semaine.")
            else:
                total = len(trades)
                wins = len([x for x in trades if x["R"]>0])
                winrate = wins/total*100
                total_R = sum([x["R"] for x in trades])
                exp = total_R/total if total>0 else 0
                tp2 = len([x for x in trades if x["result"]=="TP2"])
                be = len([x for x in trades if x["result"]=="BE"])
                sl = len([x for x in trades if x["result"]=="SL"])
                send_tg(f"📊 **BILAN HEBDO PAPER**\n\nTrades: {total}\nWinrate: {winrate:.0f}%\nTP2: {tp2} | BE: {be} | SL: {sl}\nProfit: {total_R:+.1f}R\nExpectancy: {exp:.2f}R/trade\n\nReset auto lundi.")
                save_stats({"trades": [], "week_start": str(datetime.now().date())})
            time.sleep(60)
        time.sleep(30)

def main_loop():
    send_tg("🤖 **Bot V6 Final Démarré**\nIntraday 15m Tranquille + BE + Horaires Forex + Paper + Bilan Hebdo")
    was_closed = False
    while True:
        try:
            if not is_market_open():
                if not was_closed:
                    send_tg("🌙 **Marché FERMÉ**\nPause 23h-00h ou week-end. Veille auto.")
                    was_closed = True
                time.sleep(30)
                continue
            else:
                if was_closed:
                    send_tg("☀️ **Marché OUVERT**\nXAUUSD coté. Bot réactivé.")
                    was_closed = False
            sig = check_new_signal()
            if sig:
                price_now = float(yf.Ticker(CONFIG["SYMBOL"]).fast_info['last_price'])
                order = "BUY LIMIT" if sig['side']=="LONG" and sig['entry']<price_now else "BUY STOP" if sig['side']=="LONG" else "SELL LIMIT" if sig['entry']>price_now else "SELL STOP"
                active_trades.append(sig)
                mode = "PAPER" if CONFIG["PAPER_MODE"] else "RÉEL"
                send_tg(f"🔔 **SIGNAL 15M - {mode}** {order} - {sig['side']}\nEntrée `{sig['entry']:.2f}` SL `{sig['stop']:.2f}` TP1 `{sig['tp1']:.2f}` TP2 `{sig['tp2']:.2f}`")
            manage_trades()
        except Exception as e:
            print(e)
        time.sleep(30)

threading.Thread(target=main_loop, daemon=True).start()
threading.Thread(target=weekly_report, daemon=True).start()
@app.route('/')
def home(): return "Bot V6 Final - Actif"
app.run(host='0.0.0.0', port=10000)
