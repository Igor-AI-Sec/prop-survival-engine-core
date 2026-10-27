"""Validate the complete append-only OMS history before rebuilding memory.

This detects structural corruption and missing interior records, not malicious
rewrites or a valid-looking truncated tail; the journal is not a signed archive.
"""

from uuid import UUID
from datetime import datetime

from prop_survival_engine.core import events
from prop_survival_engine.execution.events import event_from_payload, execution_from_payload, execution_payload
from prop_survival_engine.oms.models import OrderState


TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}


def validate_replay(records):
    known = {value for name, value in vars(events).items() if name.isupper()}
    orders, intents, client_ids, receipts, executions, decisions = {}, set(), set(), set(), {}, {}
    bridge_commands, cancel_commands, order_payloads, bridge_results = {}, {}, {}, set()
    for expected, record in enumerate(records, 1):
        if type(record.sequence) is not int or record.sequence != expected:
            raise ValueError("missing or reordered journal sequence")
        if record.event_type not in known or not isinstance(record.payload, dict):
            raise ValueError("unsupported journal record")
        kind, data = record.event_type, record.payload
        if kind == events.BRIDGE_EVENT:
            from prop_survival_engine.execution.bridge_protocol import validate
            validate(data["event"], event=True)
        elif kind == events.BRIDGE_COMMAND:
            from prop_survival_engine.execution.bridge_protocol import validate
            validate(data["command"])
            if data["stage"] not in {"DISPATCHED", "RESULT"}:
                raise ValueError("invalid bridge dispatch record")
            command = data["command"]
            cid = command["command_id"]
            if data["stage"] == "DISPATCHED":
                if cid in bridge_commands or data["result"] is not None:
                    raise ValueError("duplicate dispatch or premature bridge result")
                if command["kind"] == "SUBMIT_ORDER":
                    p = command["payload"]
                    original = order_payloads.get(UUID(p["order_id"]))
                    if (original is None or any(original[k] != p[k] for k in
                            ("intent_id", "client_order_id", "instrument", "side", "quantity"))
                            or orders[UUID(p["order_id"])][2] is not OrderState.SUBMITTING):
                        raise ValueError("bridge submission lacks OMS authorization")
                elif command["kind"] == "CANCEL_ORDER":
                    if cancel_commands.get(cid) != command["payload"]["client_order_id"]:
                        raise ValueError("bridge cancel lacks OMS authorization")
                elif command["kind"] in {"MODIFY_ORDER", "FLATTEN"}:
                    raise ValueError("unqualified amendment/exit dispatch")
                bridge_commands[cid] = command
            if data["stage"] == "RESULT":
                if bridge_commands.get(cid) != command or cid in bridge_results:
                    raise ValueError("bridge result without matching durable dispatch")
                validate(data["result"], event=True)
                if any(command[k] != data["result"][k] for k in
                        ("command_id", "account_id", "environment_id", "binding_version", "owner_id", "fencing_token")):
                    raise ValueError("bridge result identity mismatch")
                expected_result = {"HELLO": {"HELLO_ACK"}, "HEARTBEAT": {"HEARTBEAT"},
                    "SNAPSHOT_REQUEST": {"SNAPSHOT"}, "SUBMIT_ORDER": {"ORDER_UPDATE"}, "CANCEL_ORDER": {"ORDER_UPDATE"}}
                if data["result"]["kind"] not in expected_result[command["kind"]] | {"COMMAND_REJECTED"}:
                    raise ValueError("transport acknowledgement is not a definitive result")
                if data["result"]["kind"] == "ORDER_UPDATE":
                    original = command["payload"] if command["kind"] == "SUBMIT_ORDER" else next(
                        o for o in order_payloads.values() if o["client_order_id"] == command["payload"]["client_order_id"])
                    fact = data["result"]["payload"]
                    if (any(fact[k] != original[k] for k in ("client_order_id", "instrument", "side", "quantity"))
                            or fact.get("our_order_id") not in {None, original["order_id"]}):
                        raise ValueError("bridge result order identity mismatch")
                bridge_results.add(cid)
        elif kind == events.EXECUTION_CANCEL_REQUESTED:
            original = order_payloads.get(UUID(data["order_id"]))
            if original is None or original["client_order_id"] != data["client_order_id"]:
                raise ValueError("cancel authorization requires a journaled OMS order")
            UUID(data["command_id"])
            if data["command_id"] in cancel_commands:
                raise ValueError("duplicate cancellation authorization")
            cancel_commands[data["command_id"]] = data["client_order_id"]
        elif kind == events.INTENT_DECIDED:
            intent = UUID(data["intent_id"])
            if intent in decisions or data["action"] not in {"ALLOW", "REDUCE", "REJECT"}:
                raise ValueError("invalid intent decision")
            decisions[intent] = data["action"]
        elif kind == events.ORDER_CREATED:
            oid, intent = UUID(data["order_id"]), UUID(data["intent_id"])
            client_id, quantity = data["client_order_id"], data["quantity"]
            if (oid in orders or intent in intents or client_id in client_ids
                    or decisions.get(intent) not in {"ALLOW", "REDUCE"}
                    or not isinstance(client_id, str) or not client_id
                    or type(quantity) is not int or quantity <= 0
                    or data["side"] not in {"BUY", "SELL"}
                    or not isinstance(data["instrument"], str) or not data["instrument"]
                    or type(data["is_entry"]) is not bool):
                raise ValueError("invalid or duplicate executable order")
            orders[oid] = [quantity, 0, OrderState.CREATED]
            order_payloads[oid] = data
            intents.add(intent)
            client_ids.add(client_id)
        elif kind == events.EXECUTION_EVENT_RECEIVED:
            event = event_from_payload(data["event"])
            if data["event_id"] != event.fingerprint:
                raise ValueError("event receipt fingerprint mismatch")
            receipts.add(data["event_id"])
        elif kind == events.EXECUTION_EVENT_DECISION:
            if (data["event_id"] not in receipts
                    or data["outcome"] not in {"applied", "duplicate", "stale", "gate", "deferred"}):
                raise ValueError("decision without receipt or unsupported outcome")
            if "stream_online" in data and type(data["stream_online"]) is not bool:
                raise ValueError("invalid stream state")
            if "cursor" in data:
                timestamp = datetime.fromisoformat(data["cursor"][0])
                if timestamp.utcoffset() is None or not data["provider_order_id"]:
                    raise ValueError("invalid provider ordering cursor")
        elif kind == events.PLATFORM_POSITIONS:
            if (not isinstance(data["positions"], dict)
                    or any(not isinstance(k, str) or not k or type(v) is not int
                           for k, v in data["positions"].items())):
                raise ValueError("invalid position evidence")
        elif kind == events.RECONCILIATION and data["result"] not in {"success", "gated"}:
            raise ValueError("invalid reconciliation result")
        if kind in {events.SUBMISSION_STATE, events.PLATFORM_ORDER, events.EXECUTION_EVENT_DECISION}:
            if kind == events.SUBMISSION_STATE:
                UUID(data["order_id"])
            if kind == events.PLATFORM_ORDER and data.get("provider_timestamp"):
                if datetime.fromisoformat(data["provider_timestamp"]).utcoffset() is None:
                    raise ValueError("invalid snapshot timestamp")
            if data.get("external"):
                continue
            oid = data.get("order_id", data.get("our_order_id"))
            if oid is None and kind == events.PLATFORM_ORDER:
                # Legacy journal observations can use client_order_id only.
                if data.get("client_order_id") not in client_ids:
                    raise ValueError("unmapped platform observation")
                continue
            if oid is not None:
                quantity, prior_fill, prior_state = orders[UUID(oid)]
                state = OrderState(data.get("state", data.get("status")))
                fill = data.get("filled_quantity", prior_fill)
                if (type(fill) is not int or not prior_fill <= fill <= quantity
                        or (prior_state in TERMINAL and state is not prior_state)
                        or (state is OrderState.FILLED and fill != quantity)):
                    raise ValueError("journal lifecycle or volume regression")
                orders[UUID(oid)] = [quantity, fill, state]
        if kind == events.PLATFORM_EXECUTION or "execution" in data:
            evidence = data if kind == events.PLATFORM_EXECUTION else data["execution"]
            execution = execution_from_payload(evidence)
            if (type(execution.quantity) is not int or execution.quantity <= 0
                    or not execution.price.is_finite() or execution.timestamp.utcoffset() is None
                    or execution.side not in {"BUY", "SELL"}
                    or any(not isinstance(v, str) or not v for v in
                           (execution.execution_id, execution.provider_order_id, execution.instrument))):
                raise ValueError("invalid execution evidence")
            key = (evidence["provider_order_id"], evidence["execution_id"])
            # Local correlation can become available after the same fill.
            canonical = execution_payload(execution)
            if key in executions and executions[key] != canonical:
                raise ValueError("conflicting execution journal identity")
            executions[key] = canonical
