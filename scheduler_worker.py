"""
Nonstop continuous lead engine scheduler (no-login, active-intent).
Pipeline per cycle: discover -> qualify -> enrich -> verify -> dedup -> save -> Telegram.
Budget guard + daily target respected. Run: python scheduler_worker.py (run_scheduler.bat)
"""
import os
import sys
import time
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import engine
import telegram_bot
import config

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler.log")
INTERVAL_SECONDS = 1800         # run a discovery cycle every 30 min (nonstop)
PAUSE_IF_IDLE = 900              # extra idle wait when a cycle yields nothing new

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scheduler")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "scheduler_state.json")


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return __import__("json").load(f)
        except Exception:
            pass
    return {"last_run": None, "total_cycles": 0}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            __import__("json").dump(state, f, indent=2)
    except Exception:
        pass


def _log(msg):
    log.info(msg)
    print(msg)


def run_one_cycle(cycle_no):
    _log(f"[Cycle {cycle_no}] Engine run_cycle() starting...")
    final, stats = engine.run_cycle()
    notified = stats.get("notified", 0)
    _log(f"[Cycle {cycle_no}] final_saved={final} | "
         f"discovered={stats.get('discovered')} qualified_cycle={stats.get('qualified_cycle')} "
         f"emails={stats.get('emails_found')} verified={stats.get('verified')} "
         f"rejected={stats.get('rejected')} notified={notified} "
         f"spend=${stats.get('spend_today', 0):.4f}")
    if stats.get("stop"):
        _log(f"  stop: {stats['stop']}")
    # Note: Telegram cards for newly-saved leads are sent inside engine.run_cycle()
    # via _notify_new_lead() (idempotent: uses the all-time sent-email set). The
    # scheduler must NOT resend leads here, or the same lead would be notified
    # repeatedly. notified (above) reports cards delivered this cycle.
    return final, stats, notified


def main():
    storage.init()
    state = _load_state()
    try:
        telegram_bot.send_status("Lead engine scheduler started. Mode: no-login active-intent, "
                                 f"daily Apify cap ${config.MAX_DAILY_APIFY_SPEND:.2f}, "
                                 f"target {config.DAILY_TARGET_VERIFIED_LEADS}/day.")
    except Exception:
        pass
    _log("=== Scheduler started (no-login active-intent engine) ===")

    cycle_no = 1
    idle_streak = 0
    while True:
        started = datetime.now()
        try:
            final, stats, sent = run_one_cycle(cycle_no)
            state["last_run"] = datetime.now().isoformat()
            state["total_cycles"] = state.get("total_cycles", 0) + 1
            _save_state(state)
            did_work = (final > 0 or stats.get("qualified_cycle", 0) > 0)
            idle_streak = 0 if did_work else idle_streak + 1
        except Exception as e:
            _log(f"!!! Cycle error: {e}")
            try:
                telegram_bot.send_status(f"Engine cycle error: {e}")
            except Exception:
                pass
            idle_streak += 1

        elapsed = (datetime.now() - started).seconds
        wait = PAUSE_IF_IDLE if idle_streak >= 2 else INTERVAL_SECONDS
        _log(f"Cycle done in {elapsed}s. Sleeping {wait}s...")
        cycle_no += 1
        time.sleep(wait)


if __name__ == "__main__":
    main()
