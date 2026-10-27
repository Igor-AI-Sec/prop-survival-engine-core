"""Version 1 JSON-lines boundary. Transport sequence is not venue ordering."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from uuid import UUID

from prop_survival_engine.core.models import Side
from prop_survival_engine.core.ownership import InventoryEvidence, InventoryOrder, InventoryOrderState
from prop_survival_engine.execution.events import ExecutionEvent, ExecutionEventKind, execution_from_payload
from prop_survival_engine.execution.interfaces import PlatformOrder, PlatformOrderStatus, PlatformSnapshot

VERSION = 1
MAX_FRAME = 65536
COMMANDS = frozenset({"HELLO", "HEARTBEAT", "SNAPSHOT_REQUEST", "SUBMIT_ORDER", "MODIFY_ORDER", "CANCEL_ORDER", "FLATTEN"})
EVENTS = frozenset({"HELLO_ACK", "COMMAND_ACCEPTED", "COMMAND_REJECTED", "ORDER_UPDATE", "FILL", "POSITION_UPDATE", "SNAPSHOT", "HEARTBEAT", "ERROR"})


class ProtocolError(ValueError):
    pass


def uuid_text(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ProtocolError("UUID must use canonical lowercase hyphenated encoding")


def stamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError("timestamp must be an ISO-8601 string")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ProtocolError("timestamp must include timezone")
    return result


def money(value):
    if not isinstance(value, str):
        raise ProtocolError("price must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ProtocolError("invalid decimal") from None
    if not result.is_finite() or result <= 0:
        raise ProtocolError("price must be finite and positive")
    return result


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ProtocolError("quantity/sequence must be a whole integer")
    return value


def _exact(value):
    if isinstance(value, float):
        raise ProtocolError("binary float is forbidden on the wire")
    if isinstance(value, dict):
        for item in value.values():
            _exact(item)
    elif isinstance(value, list):
        for item in value:
            _exact(item)


def validate(frame, *, event=False):
    _exact(frame)
    required = {"protocol_version", "kind", "account_id", "environment_id", "binding_version", "owner_id", "fencing_token", "command_id", "payload"}
    if event:
        required |= {"session_id", "event_id", "sequence", "provider_timestamp", "bridge_receive_timestamp"}
    if not isinstance(frame, dict) or set(frame) != required:
        raise ProtocolError("invalid envelope fields")
    if type(frame["protocol_version"]) is not int or frame["protocol_version"] != VERSION:
        raise ProtocolError("unsupported protocol version")
    if frame["kind"] not in (EVENTS if event else COMMANDS):
        raise ProtocolError("unsupported message kind")
    for field in ("account_id", "environment_id", "binding_version"):
        if not isinstance(frame[field], str) or not frame[field]:
            raise ProtocolError("missing account binding")
    uuid_text(frame["owner_id"])
    uuid_text(frame["command_id"])
    integer(frame["fencing_token"], 1)
    if not isinstance(frame["payload"], dict):
        raise ProtocolError("payload must be an object")
    if event:
        uuid_text(frame["session_id"])
        uuid_text(frame["event_id"])
        integer(frame["sequence"], 1)
        stamp(frame["provider_timestamp"])
        if stamp(frame["bridge_receive_timestamp"]) is None:
            raise ProtocolError("missing bridge receipt timestamp")
    p, kind = frame["payload"], frame["kind"]
    if not event and kind == "SUBMIT_ORDER":
        if set(p) != {"order_id", "intent_id", "client_order_id", "instrument", "side", "quantity", "order_type", "limit_price", "stop_price"}:
            raise ProtocolError("invalid submit fields")
        uuid_text(p["order_id"]); uuid_text(p["intent_id"])
        for key in ("client_order_id", "instrument"):
            if not isinstance(p[key], str) or not p[key]:
                raise ProtocolError("missing order identity")
        if p["side"] not in {"BUY", "SELL"}:
            raise ProtocolError("invalid side")
        integer(p["quantity"], 1)
        if p["order_type"] not in {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}:
            raise ProtocolError("invalid order type")
        for key, needed in (("limit_price", p["order_type"] in {"LIMIT", "STOP_LIMIT"}), ("stop_price", p["order_type"] in {"STOP", "STOP_LIMIT"})):
            if needed:
                money(p[key])
            elif p[key] is not None:
                raise ProtocolError("unexpected price")
    elif not event and kind == "CANCEL_ORDER":
        if set(p) != {"client_order_id"} or not isinstance(p["client_order_id"], str) or not p["client_order_id"]:
            raise ProtocolError("invalid cancellation identity")
    elif not event and kind == "MODIFY_ORDER":
        if set(p) != {"client_order_id", "quantity", "limit_price", "stop_price"} or not p["client_order_id"]:
            raise ProtocolError("invalid modification")
        integer(p["quantity"], 1)
        for field in ("limit_price", "stop_price"):
            if p[field] is not None:
                money(p[field])
    elif not event and kind == "FLATTEN":
        if set(p) != {"instrument"} or not isinstance(p["instrument"], str) or not p["instrument"]:
            raise ProtocolError("flatten requires an explicit instrument")
    elif not event and p:
        raise ProtocolError("control commands have empty payloads")
    if event:
        if kind in {"HELLO_ACK", "COMMAND_ACCEPTED", "COMMAND_REJECTED", "HEARTBEAT", "ERROR"}:
            if kind == "HELLO_ACK":
                if (set(p) != {"capabilities", "qualified_amendments", "qualified_flatten"}
                        or not isinstance(p["capabilities"], list)
                        or any(not isinstance(c, str) or not c for c in p["capabilities"])
                        or type(p["qualified_amendments"]) is not bool or type(p["qualified_flatten"]) is not bool):
                    raise ProtocolError("invalid capabilities")
            elif kind in {"COMMAND_REJECTED", "ERROR"}:
                if set(p) != {"reason_code"} or not isinstance(p["reason_code"], str) or not p["reason_code"]:
                    raise ProtocolError("invalid reason code")
            elif p:
                raise ProtocolError("control response must have empty payload")
        elif kind == "POSITION_UPDATE":
            if set(p) != {"instrument", "quantity"} or not isinstance(p["instrument"], str) or not p["instrument"] or type(p["quantity"]) is not int:
                raise ProtocolError("invalid position fact")
        elif kind in {"ORDER_UPDATE", "FILL"}:
            for key in ("provider_order_id", "instrument"):
                if not isinstance(p.get(key), str) or not p[key]:
                    raise ProtocolError("missing execution identity")
            if p.get("side") not in {"BUY", "SELL"}:
                raise ProtocolError("invalid execution side")
            integer(p.get("quantity"), 1)
            if kind == "FILL":
                if (set(p) != {"execution_id", "provider_order_id", "instrument", "side", "quantity", "price"}
                        or not isinstance(p["execution_id"], str) or not p["execution_id"]):
                    raise ProtocolError("invalid execution fields")
                money(p["price"])
                if frame["provider_timestamp"] is None:
                    raise ProtocolError("fill requires a provider timestamp")
            else:
                required_order = {"client_order_id", "provider_order_id", "instrument", "side", "quantity", "filled_quantity", "status"}
                if not required_order <= set(p) or set(p) - required_order - {"our_order_id", "provider_timestamp"}:
                    raise ProtocolError("invalid order fact fields")
                PlatformOrderStatus(p["status"])
                if integer(p["filled_quantity"]) > p["quantity"] or not p["client_order_id"]:
                    raise ProtocolError("invalid cumulative fill")
                if p.get("provider_timestamp") is not None and stamp(p["provider_timestamp"]) != stamp(frame["provider_timestamp"]):
                    raise ProtocolError("inconsistent provider timestamps")
        elif kind == "SNAPSHOT":
            if set(p) != {"orders", "positions", "executions", "provider_version", "orders_complete", "positions_complete", "executions_complete"}:
                raise ProtocolError("invalid snapshot fields")
            snapshot(frame)  # Validate before journal/cache or OMS projection.
    return frame


def encode(frame, *, event=False):
    validate(frame, event=event)
    result = json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(result) > MAX_FRAME:
        raise ProtocolError("frame too large")
    return result


def decode(line, *, event=False):
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise ProtocolError("invalid frame length/delimiter")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError("duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("nonfinite JSON")))
        return validate(value, event=event)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ProtocolError("malformed protocol frame") from None


def observation(frame):
    """Never derive authoritative order timestamps from bridge receipt time."""
    p = frame["payload"]
    timestamp = stamp(frame["provider_timestamp"])
    if frame["kind"] == "FILL":
        money(p["price"]); integer(p["quantity"], 1)
        if p["side"] not in {"BUY", "SELL"} or timestamp is None:
            raise ProtocolError("invalid fill evidence")
        execution = execution_from_payload({**p, "timestamp": timestamp.isoformat()})
        return ExecutionEvent(ExecutionEventKind.EXECUTION, execution.provider_order_id, timestamp, execution=execution)
    if frame["kind"] == "ORDER_UPDATE":
        state = PlatformOrderStatus(p["status"])
        kinds = {PlatformOrderStatus.ACCEPTED: ExecutionEventKind.ACKNOWLEDGED,
                 PlatformOrderStatus.PARTIALLY_FILLED: ExecutionEventKind.PARTIAL_FILL,
                 PlatformOrderStatus.FILLED: ExecutionEventKind.FILLED,
                 PlatformOrderStatus.CANCELLED: ExecutionEventKind.CANCELLED,
                 PlatformOrderStatus.REJECTED: ExecutionEventKind.REJECTED}
        return ExecutionEvent(kinds.get(state, ExecutionEventKind.UNKNOWN), p["provider_order_id"], timestamp,
            integer(p["filled_quantity"]), order_quantity=integer(p["quantity"], 1), instrument=p["instrument"], side=p["side"])
    # The stream is online, but inventory changed and requires a fresh snapshot.
    # The complete position fact is persisted separately as BRIDGE_EVENT before
    # this lifecycle gate. It never updates the local position directly.
    return ExecutionEvent(ExecutionEventKind.STREAM_CONNECTED, occurrence_id="inventory:" + frame["event_id"])


def snapshot(frame):
    p = frame["payload"]
    if not isinstance(p["positions"], dict) or not isinstance(p["orders"], list) or not isinstance(p["executions"], list):
        raise ProtocolError("invalid inventory collections")
    for marker in ("orders_complete", "positions_complete", "executions_complete"):
        if type(p[marker]) is not bool:
            raise ProtocolError("completeness must be explicit")
    if any(not key or type(q) is not int for key, q in p["positions"].items()):
        raise ProtocolError("positions must be integer contracts")
    orders, inventory = [], []
    for row in p["orders"]:
        fields = {"client_order_id", "provider_order_id", "instrument", "side", "quantity", "filled_quantity", "status"}
        if (not isinstance(row, dict) or not fields <= set(row)
                or set(row) - fields - {"our_order_id", "provider_timestamp"}
                or any(not isinstance(row[k], str) or not row[k] for k in ("provider_order_id", "instrument"))
                or not isinstance(row["client_order_id"], str)
                or row.get("our_order_id") is not None and not isinstance(row["our_order_id"], str)):
            raise ProtocolError("invalid snapshot order fields")
        status = PlatformOrderStatus(row["status"])
        orders.append(PlatformOrder(row["client_order_id"], status, integer(row["filled_quantity"]), row["provider_order_id"],
                                    row.get("our_order_id"), provider_timestamp=stamp(row.get("provider_timestamp"))))
        mapping = {"ACCEPTED": "WORKING", "PARTIALLY_FILLED": "WORKING"}
        inventory.append(InventoryOrder(row["provider_order_id"], row["instrument"], Side(row["side"]),
            row["quantity"], row["filled_quantity"], InventoryOrderState(mapping.get(status.value, status.value)),
            row["client_order_id"], row.get("our_order_id")))
    executions = []
    for row in p["executions"]:
        fields = {"execution_id", "provider_order_id", "instrument", "side", "quantity", "price", "timestamp"}
        if (not isinstance(row, dict) or not fields <= set(row) or set(row) - fields - {"our_order_id"}
                or row.get("our_order_id") is not None and not isinstance(row["our_order_id"], str)):
            raise ProtocolError("invalid snapshot execution fields")
        for key in ("execution_id", "provider_order_id", "instrument"):
            if not isinstance(row[key], str) or not row[key]:
                raise ProtocolError("missing snapshot execution identity")
        money(row["price"]); integer(row["quantity"], 1)
        if stamp(row["timestamp"]) is None or row["side"] not in {"BUY", "SELL"}:
            raise ProtocolError("invalid snapshot execution")
        executions.append(execution_from_payload(row))
    version = p["provider_version"]
    if version is not None:
        integer(version)
    evidence = InventoryEvidence(frame["account_id"], frame["environment_id"], frame["binding_version"],
        stamp(frame["provider_timestamp"]), version, p["orders_complete"], p["positions_complete"], p["executions_complete"], tuple(inventory))
    return PlatformSnapshot(tuple(orders), dict(p["positions"]), tuple(executions), evidence)
