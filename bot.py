"""Deterministic Vera-style merchant message composer and judge API."""
from __future__ import annotations

import os
import time
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Vera Merchant Assistant")
STARTED = time.time()
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, list[dict[str, Any]]] = {}
sent_suppression_keys: set[str] = set()


def _pct(value: Any) -> str:
    try:
        return f"{abs(float(value)):.0%}"
    except (TypeError, ValueError):
        return ""


def _words(value: Any) -> str:
    return str(value).replace("_", " ").strip()


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Compose a message using only facts present in the supplied contexts."""
    kind = str(trigger.get("kind", "")).lower()
    payload = trigger.get("payload") or {}
    identity = merchant.get("identity") or {}
    name = identity.get("name") or "there"
    owner = identity.get("owner_first_name")
    greeting = f"{owner}" if owner else name
    suppression = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id', 'merchant')}"
    customer_scope = trigger.get("scope") == "customer" or customer is not None
    send_as = "merchant_on_behalf" if customer_scope else "vera"
    cta = "open_ended"

    if customer_scope:
        consent = (customer or {}).get("consent") or {}
        scope = consent.get("scope") or []
        consent_terms = " ".join(scope).lower()
        allowed_terms = {
            "recall_due": ("recall",), "customer_lapsed_soft": ("recall", "winback", "promotional"),
            "customer_lapsed_hard": ("winback", "renewal", "promotional"), "winback_eligible": ("winback", "promotional"),
            "chronic_refill_due": ("refill",), "appointment_tomorrow": ("appointment",),
            "trial_followup": ("trial", "program", "appointment"),
            "wedding_package_followup": ("wedding", "bridal", "appointment", "promotional"),
        }.get(kind, ("recall", "appointment", "promotional", "refill", "service", "program"))
        permitted = bool(consent.get("opted_in_at")) and any(term in consent_terms for term in allowed_terms)
        if not permitted:
            return {"body": "", "cta": "none", "send_as": send_as,
                    "suppression_key": suppression, "rationale": "Customer outreach requires a matching recorded opt-in."}
        person = (customer.get("identity") or {}).get("name") or "there"
        if "(parent: " in person:
            person = person.split("(parent: ", 1)[1].rstrip(")")
        slots = payload.get("available_slots") or payload.get("next_session_options") or []
        slot = slots[0].get("label") if slots and isinstance(slots[0], dict) else None
        if kind == "chronic_refill_due":
            runout = payload.get("stock_runs_out_iso")
            try:
                runout = datetime.fromisoformat(runout).strftime("%d %b") if runout else None
            except (TypeError, ValueError):
                pass
            body = f"Hi {person}, a refill reminder from {name}."
            if runout:
                body += f" Your listed regular medicines may need replenishing by {runout}."
            body += " Would you like us to confirm stock and your delivery option?"
            cta = "binary_yes_no"
        elif kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard"}:
            detail = _words(payload.get("service_due") or payload.get("previous_focus") or "your next visit")
            body = f"Hi {person}, a reminder from {name}: it may be time to follow up on {detail}."
            if slot:
                body += f" We have {slot} available. Would you like us to check a suitable time?"
            else:
                body += " Would you like us to help arrange a convenient time?"
            cta = "binary_yes_no"
        elif kind in {"appointment_tomorrow", "trial_followup", "wedding_package_followup"}:
            when = payload.get("appointment_time") or payload.get("wedding_date") or payload.get("trial_date") or payload.get("next_step_window_open")
            if when and len(str(when)) == 10 and str(when)[4] == "-":
                try:
                    when = datetime.strptime(str(when), "%Y-%m-%d").strftime("%d %b %Y")
                except ValueError:
                    pass
            body = f"Hi {person}, a quick note from {name}"
            if when:
                body += f" about {when}"
            body += ". Reply here if you need to update the plan."
            cta = "binary_yes_no"
        else:
            body = ""
            cta = "none"
        rationale = "Customer-facing follow-up uses the supplied relationship or trigger details and recorded consent."
    else:
        perf = merchant.get("performance") or {}
        metric = payload.get("metric") or "performance"
        change = payload.get("delta_pct")
        amount = _pct(change)
        window = payload.get("window") or "recent period"
        deltas = perf.get("delta_7d") or {}
        if kind in {"research_digest", "regulation_change", "cde_opportunity"}:
            item_id = payload.get("top_item_id") or payload.get("digest_item_id")
            item = next((x for x in category.get("digest", []) if x.get("id") == item_id), None)
            if item:
                if kind == "research_digest" and item.get("summary"):
                    body = f"{greeting}, {item.get('source', 'Your category digest')} reports: {item['summary']}"
                    if item.get("trial_n"):
                        body += f" ({int(item['trial_n']):,} participants)"
                    high_risk = (merchant.get("customer_aggregate") or {}).get("high_risk_adult_count")
                    if high_risk:
                        body += f". Your context lists {high_risk} high-risk adults"
                    library = category.get("patient_content_library") or []
                    if library:
                        body += ". Want me to pull the source and draft a patient WhatsApp?"
                    else:
                        body += ". Want me to share the source details?"
                else:
                    body = f"{greeting}, {item.get('title', 'a new category update')} ({item.get('source', 'source in your digest')})."
                    if item.get("actionable"):
                        body += f" {item['actionable']}."
                    elif kind == "regulation_change":
                        body += " Want a short compliance checklist?"
                if kind == "cde_opportunity":
                    body += " Want me to share the registration details?"
            else:
                topic = payload.get("topic") or payload.get("title")
                if topic:
                    body = f"{greeting}, there’s a new {kind.replace('_', ' ')} update about {topic}. Want me to share the details?"
                else:
                    body = f"{greeting}, I don’t have the details for this {kind.replace('_', ' ')} alert yet. Want me to check the source before suggesting an action?"
            if kind == "research_digest":
                cta = "open_ended"
            else:
                cta = "binary_yes_no"
        elif kind in {"perf_dip", "seasonal_perf_dip"}:
            if change is None:
                change = deltas.get(f"{metric}_pct")
                if change is None:
                    metric, change = next(((key.removesuffix("_pct"), value) for key, value in deltas.items()
                                           if isinstance(value, (int, float)) and value < 0), ("profile activity", None))
                    window = "7d"
                amount = _pct(change)
            if change is None:
                body = f"{greeting}, I don’t have a metric-specific decline in the latest snapshot. Want me to check which profile numbers changed?"
                cta = "binary_yes_no"
                return {"body": body, "cta": cta, "send_as": send_as,
                        "suppression_key": suppression,
                        "rationale": "The trigger has no decline metric and the merchant snapshot does not confirm one, so the message asks to inspect the data."}
            body = f"{greeting}, {metric} are down {amount or 'in the latest snapshot'} over {window}."
            if kind == "seasonal_perf_dip" and payload.get("season_note"):
                body += f" The context flags this as seasonal ({_words(payload['season_note'])}). Want me to check which profile update could help?"
            else:
                body += " Want me to look at the profile and suggest one change?"
            cta = "binary_yes_no"
        elif kind == "perf_spike":
            if change is None:
                change = deltas.get(f"{metric}_pct")
                if change is None:
                    metric, change = next(((key.removesuffix("_pct"), value) for key, value in deltas.items()
                                           if isinstance(value, (int, float)) and value > 0), ("profile activity", None))
                    window = "7d"
                amount = _pct(change)
            if change is None:
                body = f"{greeting}, I don’t have a metric-specific spike in the latest snapshot. Want me to check which profile numbers changed?"
                cta = "binary_yes_no"
                return {"body": body, "cta": cta, "send_as": send_as,
                        "suppression_key": suppression,
                        "rationale": "The trigger has no spike metric and the merchant snapshot does not confirm one, so the message asks to inspect the data."}
            body = f"{greeting}, {metric} rose {amount or 'in the latest snapshot'} over {window}"
            if payload.get("likely_driver"):
                body += f", alongside {str(payload['likely_driver']).replace('_', ' ')}"
            body += ". Want to build on what’s working?"
            cta = "binary_yes_no"
        elif kind in {"renewal_due", "subscription_expiry"}:
            days = payload.get("days_remaining", (merchant.get("subscription") or {}).get("days_remaining"))
            body = f"{greeting}, your {(payload.get('plan') or (merchant.get('subscription') or {}).get('plan') or 'subscription')} plan"
            body += f" is due for renewal in {days} days" if days is not None else " is due for renewal"
            body += ". Would you like the renewal details?"
            cta = "binary_yes_no"
        elif kind in {"competitor_opened", "competitor_activity"}:
            competitor = payload.get("competitor_name")
            distance = payload.get("distance_km")
            if not competitor and distance is None:
                body = f"{greeting}, the competitor alert is missing a confirmed name and distance. Want me to check the source before suggesting a response?"
                cta = "binary_yes_no"
                rationale = "The competitor trigger lacks identifying details, so the message asks to verify the alert rather than invent specifics."
                return {"body": body, "cta": cta, "send_as": send_as,
                        "suppression_key": suppression, "rationale": rationale}
            body = f"{greeting}, {competitor or 'a nearby competitor'}"
            if distance is not None:
                body += f" opened {distance} km away"
            if payload.get("their_offer"):
                body += f" with {payload['their_offer']}"
            body += ". Want to review how your profile presents your services?"
            cta = "binary_yes_no"
        elif kind in {"review_theme_emerged", "review_theme"}:
            theme = str(payload.get("theme", "a review theme")).replace('_', ' ')
            count = payload.get("occurrences_30d")
            body = f"{greeting}, {count} reviews in the last 30 days mention {theme}" if count is not None else f"{greeting}, recent reviews mention {theme}"
            if payload.get("common_quote"):
                body += f" (for example, ‘{payload['common_quote']}’)"
            body += ". I can help draft a thoughtful response."
            cta = "binary_yes_no"
        elif kind in {"milestone_reached", "milestone"}:
            metric_name = str(payload.get("metric", "milestone")).replace('_', ' ')
            value = payload.get("value_now", payload.get("milestone_value"))
            target = payload.get("milestone_value")
            if payload.get("is_imminent") and target is not None and value is not None:
                metric_label = "reviews" if metric_name == "review count" else _words(metric_name)
                body = f"{greeting}, you’re {target - value} {metric_label} away from {target}. Want a short thank-you post ready for when you reach it?"
            else:
                if value is None:
                    body = f"{greeting}, I can help mark your latest milestone with a short thank-you post. Want me to draft one?"
                else:
                    body = f"{greeting}, a quick milestone: {value} {_words(metric_name)}. Would you like a short thank-you post to mark it?"
            cta = "binary_yes_no"
        elif kind in {"festival_upcoming", "category_seasonal", "ipl_match_today", "local_news_event", "weather_heatwave"}:
            event = payload.get("festival") or payload.get("season") or payload.get("match") or payload.get("event") or payload.get("headline") or kind.replace('_', ' ')
            if kind == "category_seasonal" and category.get("slug") == "pharmacies" and payload.get("trends"):
                trends = ", ".join(_words(x.replace("_", " ")) for x in payload["trends"][:3])
                body = f"{greeting}, your summer category snapshot flags {trends}. Worth checking shelf availability before demand shifts. Want me to draft a short profile update?"
                cta = "binary_yes_no"
                rationale = "Uses the supplied pharmacy demand signals and avoids recommending an unrelated catalog offer."
                return {"body": body, "cta": cta, "send_as": send_as,
                        "suppression_key": suppression, "rationale": rationale}
            # Prefer a confirmed merchant offer; use catalog ideas only when
            # clearly framed as suggestions so we do not imply they are live.
            live_offers = [o.get("title") for o in merchant.get("offers", [])
                           if o.get("status") == "active" and o.get("title")]
            offer = live_offers[0] if live_offers else None
            catalog_offers = category.get("offer_catalog", [])
            catalog_idea = next((o.get("title") for o in catalog_offers
                                 if o.get("type") == "service_at_price" and o.get("title")), None)
            if not catalog_idea:
                catalog_idea = next((o.get("title") for o in catalog_offers
                                     if o.get("title") and o.get("type") != "discount"), None)
            body = f"{greeting}, {_words(event)}"
            if str(event).replace("_", " ") == "festival upcoming":
                body = f"{greeting}, which upcoming festival are you planning for? I can draft a timely post for your business once I have the event."
                cta = "open_ended"
            else:
                cta = "binary_yes_no"
            if payload.get("date"):
                body += f" is on {payload['date']}"
            if str(event).replace("_", " ") == "festival upcoming":
                pass
            else:
                body += "."
            if offer and str(event).replace("_", " ") != "festival upcoming":
                body += f" Your active offer, {offer}, could fit."
            elif catalog_idea and str(event).replace("_", " ") != "festival upcoming":
                body += f" One category-fit idea to consider is {catalog_idea}."
            if str(event).replace("_", " ") != "festival upcoming":
                body += " Want me to draft a timely profile post?"
        elif kind in {"dormant_with_vera", "curious_ask_due"}:
            if kind == "curious_ask_due":
                live_offers = [o.get("title") for o in merchant.get("offers", [])
                               if o.get("status") == "active" and o.get("title")]
                if live_offers:
                    selected_offers = live_offers[:2]
                    offer_text = " and ".join(selected_offers)
                    verb = "are" if len(selected_offers) > 1 else "is"
                    body = f"{greeting}, your {offer_text} {verb} live. Which one are customers asking about most this week? I can turn it into a focused profile post."
                else:
                    body = f"{greeting}, what service are customers asking about most this week? I can draft a profile post around the one you choose."
                cta = "open_ended"
            else:
                days = payload.get("days_since_last_merchant_message")
                body = f"{greeting}, checking in after {days} days" if days is not None else f"{greeting}, checking in"
                body += ". Is there one profile or customer-growth task I can take off your plate?"
                cta = "open_ended"
        elif kind in {"gbp_unverified", "profile_incomplete"}:
            body = f"{greeting}, your Google Business Profile is still unverified."
            uplift = payload.get("estimated_uplift_pct")
            if isinstance(uplift, (int, float)):
                body += f" This trigger estimates a {_pct(uplift)} uplift after verification."
            if payload.get("verification_path"):
                body += f" The available route is {str(payload['verification_path']).replace('_', ' ')}."
            body += " Want the next steps?"
            cta = "binary_yes_no"
        elif kind in {"supply_alert", "product_recall", "recall_alert"}:
            batches = ", ".join(payload.get("affected_batches", []))
            body = f"{greeting}, supply alert for {payload.get('molecule', 'a listed product')}"
            if batches:
                body += f"; affected batches: {batches}"
            body += ". Please check the source notice before taking stock action."
            cta = "none"
        elif kind in {"active_planning_intent", "planning_intent"}:
            topic = str(payload.get("intent_topic", "the plan")).replace('_', ' ')
            if merchant.get("category_slug") == "gyms" and "kids yoga" in topic:
                deliverable = "an age range, session outline, and parent-facing launch message"
            elif merchant.get("category_slug") == "restaurants" and "thali" in topic:
                deliverable = "a package outline and enquiry message for nearby offices"
            else:
                deliverable = "a first draft for your review"
            body = f"{greeting}, picking up your plan for {topic}. I can prepare {deliverable} now and send it for your approval. Shall I proceed?"
            cta = "binary_yes_no"
        elif kind == "winback_eligible":
            body = f"{greeting}, your profile has been inactive for {payload.get('days_since_expiry', 'a while')} days, while the context shows {payload.get('lapsed_customers_added_since_expiry', 'more')} lapsed customers. Want to see a simple restart plan?"
            cta = "binary_yes_no"
        else:
            body = f"{greeting}, there’s an update about {kind.replace('_', ' ') or 'your business'}. Want me to share the relevant details?"
        rationale = f"Responds to the {kind or 'current'} trigger with available merchant and category facts."

    return {"body": body, "cta": cta, "send_as": send_as,
            "suppression_key": suppression, "rationale": rationale}


class ContextRequest(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str
    received_at: str = ""
    turn_number: int = 1


@app.get("/v1/healthz")
def healthz():
    counts = {scope: 0 for scope in ("category", "merchant", "customer", "trigger")}
    for scope, _ in contexts:
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - STARTED), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    members = [name.strip() for name in os.environ.get("TEAM_MEMBERS", "").split(",") if name.strip()]
    return {"team_name": os.environ.get("TEAM_NAME", "Vera Composer"), "team_members": members,
            "model": "deterministic-rules",
            "approach": "Context-grounded deterministic composition with trigger routing",
            "contact_email": os.environ.get("CONTACT_EMAIL", ""),
            "version": os.environ.get("APP_VERSION", "0.1.0"),
            "submitted_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/context")
def push_context(body: ContextRequest, response: Response):
    if body.scope not in {"category", "merchant", "customer", "trigger"}:
        response.status_code = 400
        return {"accepted": False, "reason": "invalid_scope", "details": body.scope}
    key = (body.scope, body.context_id)
    existing = contexts.get(key)
    if existing and existing["version"] > body.version:
        response.status_code = 409
        return {"accepted": False, "reason": "stale_version", "current_version": existing["version"]}
    if existing and existing["version"] == body.version:
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
                "stored_at": datetime.now(timezone.utc).isoformat()}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/tick")
def tick(body: TickRequest):
    actions = []
    for trigger_id in body.available_triggers:
        trigger = contexts.get(("trigger", trigger_id), {}).get("payload")
        if not trigger:
            continue
        merchant_id = trigger.get("merchant_id")
        merchant = contexts.get(("merchant", merchant_id), {}).get("payload")
        if not merchant:
            continue
        category = contexts.get(("category", merchant.get("category_slug")), {}).get("payload", {})
        customer_id = trigger.get("customer_id")
        customer = contexts.get(("customer", customer_id), {}).get("payload") if customer_id else None
        result = compose(category, merchant, trigger, customer)
        if not result["body"]:
            continue
        if result["suppression_key"] in sent_suppression_keys:
            continue
        actions.append({"conversation_id": f"conv_{uuid4().hex[:12]}", "merchant_id": merchant_id,
                        "customer_id": customer_id, "send_as": result["send_as"], "trigger_id": trigger_id,
                        "template_name": f"vera_{trigger.get('kind', 'update')}_v1",
                        "template_params": [merchant.get("identity", {}).get("name", "there"), result["body"]],
                        **result})
        sent_suppression_keys.add(result["suppression_key"])
        if len(actions) >= 20:
            break
    return {"actions": actions}


@app.post("/v1/reply")
def reply(body: ReplyRequest):
    text = body.message.strip()
    low = text.lower()
    history = conversations.setdefault(body.conversation_id, [])
    history.append({"role": body.from_role, "message": text, "turn": body.turn_number})
    normalized = re.sub(r"\s+", " ", low.replace("’", "'")).strip()
    if (normalized.strip(".! ,") in {"stop", "unsubscribe", "opt out", "opt-out"}
            or re.search(r"\b(?:no thanks|not interested|remove me|don't (?:message|contact|text) me|do not (?:message|contact|text) me|stop (?:messaging|contacting|texting))\b", normalized)):
        return {"action": "end", "rationale": "Honoring the merchant's request to stop or decline."}
    auto_phrases = ("thank you for contacting", "thanks for contacting", "thank you for reaching out",
                    "we will get back to you", "we'll get back to you", "our team will respond",
                    "automated response", "auto-reply", "business hours are")
    is_auto = lambda message: any(phrase in message.lower() for phrase in auto_phrases)
    prior_auto = sum(1 for turn in history if is_auto(turn["message"]))
    normalized_replies = [re.sub(r"\s+", " ", turn["message"].strip().lower())
                          for turn in history if turn["role"] == body.from_role]
    repeated_verbatim = len(normalized) >= 20 and normalized_replies.count(normalized) >= 3
    if is_auto(text) or prior_auto >= 2 or repeated_verbatim:
        if prior_auto >= 2 or repeated_verbatim or body.turn_number >= 4:
            return {"action": "end", "rationale": "Repeated canned auto-reply detected; ending to avoid consuming the merchant's time."}
        return {"action": "wait", "wait_seconds": 3600, "rationale": "This appears to be a canned business auto-reply; pausing instead of treating it as intent."}
    if re.search(r"\b(?:yes|yep|yeah|sure|proceed|go ahead|please do|start|let's do it|lets do it|what's next|whats next)\b", normalized):
        return {"action": "send", "body": "Great, I’ll get that started. I’ll use the details already shared and send you the first draft or next step here for review.",
                "cta": "none", "rationale": "Merchant accepted; switching directly from qualification to action."}
    return {"action": "send", "body": "Understood. I can help with the Vera profile or customer-growth task we were discussing; tell me which detail you’d like me to handle first.",
            "cta": "open_ended", "rationale": "Keeps the conversation focused and asks one low-effort next question."}
