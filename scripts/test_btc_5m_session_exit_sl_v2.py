#!/usr/bin/env python3
"""BTC 5m V2 safety gate. PAPER is default; --execute calls the external runner."""
import argparse, datetime as dt, json, os, subprocess, time
from pathlib import Path
from typing import Any
import requests
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

UTC = dt.timezone.utc
PROFILES = {"conservative": {"threshold": .70, "stake": 5., "cap": 5., "loss": 10., "trades": 12},
            "aggressive": {"threshold": .70, "stake": 5., "cap": 5., "loss": 15., "trades": 20}}

def ts(): return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
def field(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except json.JSONDecodeError: return v
    return v

def market():
    slot = int(time.time()) // 300 * 300
    r = requests.get("https://gamma-api.polymarket.com/events", params={"slug": f"btc-updown-5m-{slot}"}, timeout=8); r.raise_for_status()
    events = r.json()
    if not events or not events[0].get("markets"): return None
    m = events[0]["markets"][0]
    if m.get("closed") or m.get("active") is False: return None
    m["_slug"] = str(m.get("slug") or events[0].get("slug")); m["_end"] = str(m.get("endDate") or m.get("endDateIso") or "")
    return m

def token_ids(m):
    labels = [str(x).lower() for x in (field(m.get("outcomes")) or [])]; ids = [str(x) for x in (field(m.get("clobTokenIds")) or [])]
    if len(ids) < 2: raise RuntimeError("missing_clob_token_ids")
    up = 1 if len(labels) > 1 and ("up" in labels[1] or "yes" in labels[1]) else 0
    return ids[up], ids[1-up]

def px(x): return float(getattr(x, "price", 0) or 0)
def qty(x): return float(getattr(x, "size", getattr(x, "quantity", 0)) or 0)
def metric(book):
    bids, asks = getattr(book, "bids", []) or [], getattr(book, "asks", []) or []
    bid, ask = max((px(x) for x in bids), default=None), min((px(x) for x in asks), default=None)
    top = min(asks, key=px) if asks else None
    return {"bid": bid, "ask": ask, "spread": ask-bid if bid is not None and ask is not None else None,
            "top_ask_notional": px(top)*qty(top) if top else 0., "bid_notional": sum(px(x)*qty(x) for x in bids), "ask_notional": sum(px(x)*qty(x) for x in asks)}
def books(up, down):
    c = ClobClient(host="https://clob.polymarket.com", chain_id=POLYGON)
    return {"UP": metric(c.get_order_book(up)), "DOWN": metric(c.get_order_book(down))}
def btc_move(lookback):
    r = requests.get("https://api.binance.com/api/v3/klines", params={"symbol":"BTCUSDT", "interval":"1m", "limit":6}, timeout=8); r.raise_for_status()
    rows = [(int(x[0])/1000, float(x[4])) for x in r.json()]; old = [x for x in rows if x[0] <= time.time()-lookback]
    if not old: raise RuntimeError("insufficient_btc_history")
    return {"start": old[-1][1], "end": rows[-1][1], "move_usd": rows[-1][1]-old[-1][1]}
def load_state(path):
    day = dt.datetime.now(UTC).date().isoformat()
    try: value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError): value = {}
    return value if value.get("day") == day else {"day":day,"trades":0,"realized_pnl_usdc":0.,"paper_candidates":0}
def save_state(path, state): path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(state, indent=2)+"\\n")
def call_external(repo, slug, side, stake):
    command = [".venv/bin/python","src/live/pm_live_trade_runner.py","--market-slug",slug,"--force-side",side,"--start-equity","100","--risk-frac",str(stake/100),"--max-notional-usd",str(stake),"--execute"]
    env = os.environ.copy(); env.update({"PM_MAX_SPREAD":".03","PM_MIN_TOP_ASK_NOTIONAL_USD":"30","PM_ORDER_TYPE":"FAK"})
    p = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=env); return p.returncode, ((p.stdout or "")+"\\n"+(p.stderr or ""))[-4000:]
def main():
    p = argparse.ArgumentParser(description="BTC 5m V2: paper by default")
    p.add_argument("--profile", choices=PROFILES, default="conservative"); p.add_argument("--repo", default=os.getenv("BTC5M_REPO","pm-hl-conservative-plus-repo"))
    p.add_argument("--stake-usd",type=float); p.add_argument("--max-notional-usd",type=float); p.add_argument("--threshold",type=float); p.add_argument("--daily-max-loss-usd",type=float); p.add_argument("--max-trades-per-day",type=int)
    p.add_argument("--btc-move-min-usd",type=float,default=70.); p.add_argument("--btc-move-reference-usd",type=float,default=100.); p.add_argument("--btc-lookback-sec",type=int,default=120)
    p.add_argument("--min-market-skew",type=float,default=.10); p.add_argument("--min-book-imbalance",type=float,default=1.); p.add_argument("--max-spread",type=float,default=.03); p.add_argument("--min-top-ask-notional-usd",type=float,default=30.); p.add_argument("--quote-max-age-sec",type=float,default=8.)
    p.add_argument("--max-api-errors",type=int,default=3); p.add_argument("--min-entry-seconds-left",type=int,default=60); p.add_argument("--entry-timeout-min",type=int,default=60); p.add_argument("--poll-sec",type=float,default=5.)
    p.add_argument("--state-file",type=Path,default=Path("var/btc5m_v2_state.json")); p.add_argument("--report-file",type=Path,default=Path("var/btc5m_v2_reports.jsonl")); p.add_argument("--execute",action="store_true")
    a=p.parse_args(); prof=PROFILES[a.profile]
    for attr,key in (("threshold","threshold"),("stake_usd","stake"),("max_notional_usd","cap"),("daily_max_loss_usd","loss"),("max_trades_per_day","trades")):
        if getattr(a,attr) is None: setattr(a,attr,prof[key])
    if a.stake_usd <= 0 or a.stake_usd > a.max_notional_usd: p.error("stake-usd must be positive and <= max-notional-usd")
    state, attempts, errors = load_state(a.state_file), [], 0; report={"started_at":ts(),"mode":"LIVE" if a.execute else "PAPER","parameters":vars(a),"attempts":attempts}
    deadline=time.time()+a.entry_timeout_min*60
    while time.time() < deadline:
        if state["trades"] >= a.max_trades_per_day: report["result"]="skip_max_trades_per_day"; break
        if state["realized_pnl_usdc"] <= -a.daily_max_loss_usd: report["result"]="skip_daily_loss_limit"; break
        try:
            m=market()
            if not m: raise RuntimeError("no_active_current_market")
            left=dt.datetime.fromisoformat(m["_end"].replace("Z","+00:00")).timestamp()-time.time()
            if left < a.min_entry_seconds_left: raise RuntimeError("too_late_to_enter")
            btc=btc_move(a.btc_lookback_sec)
            if abs(btc["move_usd"]) < a.btc_move_min_usd: raise RuntimeError("btc_momentum_below_minimum")
            side="UP" if btc["move_usd"] > 0 else "DOWN"; up,down=token_ids(m); started=time.time(); data=books(up,down); age=time.time()-started; chosen,other=data[side],data["DOWN" if side=="UP" else "UP"]
            skew=float(chosen["ask"] or 0)-float(other["ask"] or 0); imbalance=float(chosen["bid_notional"] or 0)/max(float(chosen["ask_notional"] or 0),.01)
            checks={"threshold":float(chosen["ask"] or 0)>=a.threshold,"spread":chosen["spread"] is not None and float(chosen["spread"])<=a.max_spread,"top_ask_liquidity":float(chosen["top_ask_notional"] or 0)>=a.min_top_ask_notional_usd,"market_skew":skew>=a.min_market_skew,"orderbook_imbalance":imbalance>=a.min_book_imbalance,"quote_fresh":age<=a.quote_max_age_sec}
            decision={"ts":ts(),"market_slug":m["_slug"],"side":side,"seconds_left":left,"btc":btc,"books":data,"skew":skew,"imbalance":imbalance,"checks":checks}; attempts.append(decision)
            if not all(checks.values()): raise RuntimeError("confirmation_failed")
            state["paper_candidates"] += 1
            if not a.execute: decision["status"]="paper_entry"; report["entry"]={"simulated":True,"side":side,"price":chosen["ask"],"notional_usd":a.stake_usd}; report["result"]="paper_entry"; break
            code,out=call_external(a.repo,m["_slug"],side,a.stake_usd); report["external_execution"]={"return_code":code,"output":out}; report["result"]="external_execution_called"; break
        except Exception as e:
            errors += 1; attempts.append({"ts":ts(),"status":"skip","reason":str(e),"consecutive_api_or_data_errors":errors})
            if errors >= a.max_api_errors: report["result"]="skip_api_error_limit"; break
            time.sleep(a.poll_sec)
    else: report["result"]="no_entry_timeout"
    report["finished_at"]=ts(); report["statistics"]={"attempts":len(attempts),"skips":sum(x.get("status")=="skip" for x in attempts),"paper_candidates_today":state["paper_candidates"],"trades_today":state["trades"],"realized_pnl_usdc_today":state["realized_pnl_usdc"]}
    save_state(a.state_file,state); a.report_file.parent.mkdir(parents=True,exist_ok=True)
    with a.report_file.open("a") as f: f.write(json.dumps(report,default=str,ensure_ascii=False)+"\\n")
    print(json.dumps(report,default=str,ensure_ascii=False,indent=2))
if __name__ == "__main__": main()
