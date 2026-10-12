"""The phone apps read the monitoring API; these tests pin what they read.

Neither app is built in CI (no Xcode, no Android SDK), so a renamed or
dropped API field would only show up on someone's phone. Worse, on iOS a
*non-optional* field that is missing or null makes ``JSONDecoder`` reject the
whole snapshot, so the dashboard never loads. Both apps' field lists are read
straight from their sources, so the check cannot drift from the code.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from maya.api.server import build_snapshot, json_safe
from maya.config import RunMode, Settings
from maya.data.ingestion import build_feed
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes
from tests.test_scanner import _scanner, _settings, setup_candles, short_history  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SWIFT_MODELS = ROOT / "ios" / "MayaMonitor.swiftpm" / "Models.swift"
ANDROID = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "maya" / "monitor" / "MainActivity.java"


def _snake(name: str) -> str:
    """Inverse of Swift's ``convertFromSnakeCase`` for our field names."""
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def _swift_structs() -> dict[str, list[tuple[str, str, bool]]]:
    """``{struct: [(json_key, swift_type, optional)]}`` for the Decodable models."""
    src = SWIFT_MODELS.read_text()
    out: dict[str, list[tuple[str, str, bool]]] = {}
    for m in re.finditer(r"struct (\w+)(?:<[^>]*>)?: [^{]*Decodable[^{]*\{(.*?)\n\}", src, re.S):
        fields = re.findall(r"^\s*let (\w+): ([\w\[\]]+)(\?)?", m.group(2), re.M)
        out[m.group(1)] = [(_snake(n), t, bool(opt)) for n, t, opt in fields]
    return out


def _roundtrip(obj):
    """What the app actually receives: the JSON the server writes."""
    return json.loads(json.dumps(json_safe(obj), allow_nan=False))


@pytest.fixture(scope="module")
def single_agent(tmp_path_factory):
    """Snapshot + trades after a real scripted trade (flat, run metrics full)."""
    settings = Settings(mode=RunMode.SIM, scenario=True, max_candles=0)
    journal = TradeJournal(db_path=str(tmp_path_factory.mktemp("j") / "j.db"))
    nodes = TradingNodes(settings, journal)
    graph = build_graph(nodes)
    processed = 0
    for candle in build_feed(settings).candles():
        graph.invoke({"candle": candle})
        processed += 1
    snap = _roundtrip(build_snapshot(settings, nodes, journal, processed))
    trades = _roundtrip(journal.recent(6))
    fresh = _roundtrip(build_snapshot(settings, TradingNodes(
        settings, TradeJournal(db_path=":memory:")), TradeJournal(db_path=":memory:"), 0))
    journal.close()
    assert trades, "the scripted scenario should have closed a trade"
    return snap, trades, fresh


@pytest.fixture
def scanner_agent(setup_candles):  # noqa: F811
    """Snapshot from the scanner agent: an open position and a scan list."""
    from maya.execution.engine import ExecutionEngine
    from maya.risk.manager import RiskManager
    from maya.scan_run import ScanAgent

    settings = _settings()
    risk = RiskManager(settings, 1000)
    scanner = _scanner(setup_candles)
    scanner.risk = risk
    journal = TradeJournal(db_path=":memory:")
    agent = ScanAgent(settings, journal, scanner, ExecutionEngine(settings), risk)
    agent.step(now_ts=setup_candles[-1].timestamp)
    return _roundtrip(build_snapshot(settings, agent, journal, agent.steps))


def _samples(single_agent, scanner_agent) -> dict[str, list[dict]]:
    snap, trades, fresh = single_agent
    snaps = [snap, fresh, scanner_agent]
    assert scanner_agent["position"] is not None and scanner_agent["scan"]
    return {
        "Snapshot": snaps,
        "Config": [s["config"] for s in snaps],
        "Account": [s["account"] for s in snaps],
        "Position": [scanner_agent["position"]],
        # Before the first trade `run` is just {"trades": 0}.
        "RunStats": [s["run"] for s in snaps] + [s["lifetime"] for s in snaps],
        "ScanEntry": scanner_agent["scan"],
        "Trade": trades,
    }


def _type_ok(value, swift_type: str) -> bool:
    if swift_type == "Int":
        return isinstance(value, int) and not isinstance(value, bool)
    if swift_type == "Double":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if swift_type == "String":
        return isinstance(value, str)
    if swift_type == "Bool":
        return isinstance(value, bool)
    if swift_type == "[String]":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return True  # nested model: checked under its own struct name


def test_ios_models_decode_every_snapshot_the_api_can_send(single_agent, scanner_agent):
    structs = _swift_structs()
    samples = _samples(single_agent, scanner_agent)
    assert set(samples) <= set(structs), "a model the test expects is gone from Models.swift"

    problems = []
    for name, objs in samples.items():
        for key, swift_type, optional in structs[name]:
            for obj in objs:
                value = obj.get(key)
                if value is None:
                    if not optional:
                        problems.append(f"{name}.{key}: missing/null but non-optional in Swift")
                elif not _type_ok(value, swift_type):
                    problems.append(f"{name}.{key}: {value!r} does not decode as {swift_type}")
            if not any(key in obj for obj in objs):
                problems.append(f"{name}.{key}: the API never sends this field")
    assert not problems, "\n".join(sorted(set(problems)))


def test_android_reads_only_fields_the_api_sends(single_agent, scanner_agent):
    keys: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            keys.update(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(list(_samples(single_agent, scanner_agent).values()))
    walk({"ok": True, "data": None})  # the envelope

    # JSON reads only; prefs.getString("host") etc. are the app's own settings.
    read = set(re.findall(
        r'(?<!prefs\.)\b(?:get|opt|isNull)\w*\("([a-z_]+)"', ANDROID.read_text()))
    assert read, "the key pattern no longer matches MainActivity"
    assert not read - keys, f"Android reads fields the API never sends: {sorted(read - keys)}"
