import os
import stripe
from fastapi import HTTPException

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

PLANS = {
    "creator": {
        "name": "Creator",
        "price": 9,
        "price_id": os.environ.get("STRIPE_CREATOR_PRICE_ID", ""),
        "minutes_per_month": 200,
    },
    "pro": {
        "name": "Pro",
        "price": 19,
        "price_id": os.environ.get("STRIPE_PRO_PRICE_ID", ""),
        "minutes_per_month": 600,
    },
}


def create_checkout_session(plan_key: str, user_email: str, success_url: str, cancel_url: str) -> str:
    if plan_key not in PLANS:
        raise HTTPException(400, f"Invalid plan: {plan_key}")
    plan = PLANS[plan_key]
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": plan["price_id"], "quantity": 1}],
        customer_email=user_email,
        success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=cancel_url,
        subscription_data={"metadata": {"plan": plan_key}},
        metadata={"plan": plan_key, "email": user_email},
    )
    return session.url


def create_billing_portal_session(stripe_customer_id: str, return_url: str) -> str:
    session = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=return_url,
    )
    return session.url


def handle_webhook(payload: bytes, sig_header: str) -> dict:
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid webhook signature")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        return {
            "action": "subscription_activated",
            "customer_id": data.get("customer"),
            "email": data.get("customer_email"),
            "plan": data.get("metadata", {}).get("plan", "free"),
        }
    elif event_type == "customer.subscription.deleted":
        return {"action": "subscription_cancelled", "customer_id": data.get("customer")}
    elif event_type == "invoice.payment_failed":
        return {"action": "payment_failed", "customer_id": data.get("customer")}

    return {"action": "unhandled", "type": event_type}
