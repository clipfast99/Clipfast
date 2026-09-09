import json
import os
from pathlib import Path
from threading import Lock
from datetime import datetime, timezone

USERS_DIR = Path("users")
USERS_DIR.mkdir(exist_ok=True)
_lock = Lock()

PLAN_LIMITS = {
    "free":    {"videos": 1,    "minutes": 9999},
    "creator": {"videos": 9999, "minutes": 200},
    "pro":     {"videos": 9999, "minutes": 600},
}


def _path(clerk_user_id: str) -> Path:
    safe = clerk_user_id.replace("_", "-").replace("|", "-")
    return USERS_DIR / f"{safe}.json"


def get_or_create(clerk_user_id: str, email: str = "", name: str = "") -> dict:
    path = _path(clerk_user_id)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    user = {
        "clerk_user_id": clerk_user_id,
        "email": email,
        "name": name,
        "plan": "free",
        "stripe_customer_id": None,
        "videos_processed": 0,
        "minutes_used_this_month": 0,
        "billing_period_start": _current_month(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(clerk_user_id, user)
    return user


def upgrade(clerk_user_id: str, plan: str, stripe_customer_id: str) -> dict:
    user = get_or_create(clerk_user_id)
    user["plan"] = plan
    user["stripe_customer_id"] = stripe_customer_id
    user["minutes_used_this_month"] = 0
    user["billing_period_start"] = _current_month()
    _save(clerk_user_id, user)
    return user


def downgrade(clerk_user_id: str) -> dict:
    user = get_or_create(clerk_user_id)
    user["plan"] = "free"
    user["stripe_subscription_id"] = None
    _save(clerk_user_id, user)
    return user


def get_by_stripe_customer(customer_id: str) -> dict | None:
    for path in USERS_DIR.glob("*.json"):
        with open(path) as f:
            user = json.load(f)
        if user.get("stripe_customer_id") == customer_id:
            return user
    return None


def can_process(clerk_user_id: str) -> tuple[bool, str]:
    user = get_or_create(clerk_user_id)
    _reset_if_new_month(clerk_user_id, user)
    plan = user.get("plan", "free")
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    if plan == "free":
        if user.get("videos_processed", 0) >= limits["videos"]:
            return False, "Your free video has been used. Upgrade to process more videos."
    return True, "ok"


def record_usage(clerk_user_id: str, minutes_used: float) -> dict:
    user = get_or_create(clerk_user_id)
    _reset_if_new_month(clerk_user_id, user)
    user["videos_processed"] = user.get("videos_processed", 0) + 1
    if user.get("plan", "free") != "free":
        user["minutes_used_this_month"] = user.get("minutes_used_this_month", 0) + minutes_used
    _save(clerk_user_id, user)
    return user


def get_status(clerk_user_id: str) -> dict:
    user = get_or_create(clerk_user_id)
    _reset_if_new_month(clerk_user_id, user)
    plan = user.get("plan", "free")
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    if plan == "free":
        videos_done = user.get("videos_processed", 0)
        return {
            "plan": plan,
            "videos_left": max(0, limits["videos"] - videos_done),
            "minutes_limit": None,
            "minutes_used": None,
            "minutes_remaining": None,
        }
    used = user.get("minutes_used_this_month", 0)
    limit = limits["minutes"]
    return {
        "plan": plan,
        "videos_left": 9999,
        "minutes_limit": limit,
        "minutes_used": round(used, 1),
        "minutes_remaining": round(max(0, limit - used), 1),
    }


def _reset_if_new_month(clerk_user_id: str, user: dict) -> None:
    current = _current_month()
    if user.get("billing_period_start") != current:
        user["minutes_used_this_month"] = 0
        user["billing_period_start"] = current
        _save(clerk_user_id, user)


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _save(clerk_user_id: str, user: dict) -> None:
    with _lock:
        with open(_path(clerk_user_id), "w") as f:
            json.dump(user, f, indent=2)
