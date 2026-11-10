"""Token usage and cost of peer runs, read from porch's raw backend streams.

Every porch run keeps each agent's raw stream under `$PORCH_RUN_DIR/raw/`.
This module sums the tokens each backend reports there and prices them:

- OpenCode and Claude Code report a cost themselves; that number is used.
- Codex, Antigravity and the rest report tokens only; they are priced with
  OpenRouter's public list prices (input, cached input, output), fetched
  once a day and cached, or with prices you set in the pricing file.

A run is either `metered` (pay per token: the cost is real spend) or
`subscription` (covered by a flat plan such as ChatGPT for Codex: the cost
is a list-price equivalent, useful to compare peers, not money spent).

Pricing file (optional): $PEER_HANDOFF_HOME/pricing.json
{
  "billing":  {"codex-cli": "subscription", "agent-id": "metered"},
  "models":   {"gpt-6.1-sol": "openai/gpt-6.1-sol"},
  "prices":   {"some-model": {"input": 2.0, "cached": 0.2, "output": 10.0}}
}
Keys of "billing" are agent ids or backend names; "models" maps a porch model
to an OpenRouter model id; "prices" are USD per million tokens and win over
the catalogue. Standard library only.
"""
from __future__ import annotations

import json
import re
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CATALOGUE_URL = os.environ.get("PEER_HANDOFF_PRICES_URL", "https://openrouter.ai/api/v1/models")
CATALOGUE_MAX_AGE = 24 * 3600
DEFAULT_BILLING = {"codex-cli": "subscription", "claude-code": "subscription"}
# Effort suffixes some CLIs put in the model name; list prices do not depend on them.
OPENCODE_OWN_PROVIDERS = ("opencode-go", "opencode")
EFFORT_SUFFIXES = ("-xhigh", "-high", "-medium", "-low", "-minimal")
BACKEND_VENDOR = {"codex-cli": "openai", "antigravity": "google", "gemini-cli": "google",
                  "claude-code": "anthropic", "grok-build": "x-ai"}


def _home() -> Path:
    return Path(os.environ.get("PEER_HANDOFF_HOME", str(Path.home() / ".peer-handoff"))).expanduser()


def load_pricing_file() -> Dict[str, Any]:
    path = _home() / "pricing.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ------------------------------------------------------------------ streams

def _events(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def empty_usage() -> Dict[str, Any]:
    return {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "reported_cost": None, "calls": 0}


def extract_usage(raw: Path, backend: str) -> Dict[str, Any]:
    """Sum the tokens one agent's raw stream reports.

    `input` excludes cached input; `cached` is cache reads; `output` includes
    reasoning where the backend folds it in (`reasoning` is informational).
    """
    u = empty_usage()
    cost = 0.0
    has_cost = False
    for e in _events(raw):
        if backend == "opencode" and e.get("type") == "step_finish":
            part = e.get("part") or {}
            t = part.get("tokens") or {}
            u["input"] += int(t.get("input") or 0)
            u["output"] += int(t.get("output") or 0) + int(t.get("reasoning") or 0)
            u["reasoning"] += int(t.get("reasoning") or 0)
            u["cached"] += int((t.get("cache") or {}).get("read") or 0)
            u["input"] += int((t.get("cache") or {}).get("write") or 0)
            u["calls"] += 1
            if part.get("cost") is not None:
                cost += float(part["cost"])
                has_cost = True
        elif backend == "codex-cli" and e.get("type") == "turn.completed":
            t = e.get("usage") or {}
            cached = int(t.get("cached_input_tokens") or 0)
            u["input"] += max(int(t.get("input_tokens") or 0) - cached, 0)
            u["cached"] += cached
            u["output"] += int(t.get("output_tokens") or 0)
            u["reasoning"] += int(t.get("reasoning_output_tokens") or 0)
            u["calls"] += 1
        elif backend == "antigravity" and isinstance(e.get("step_update"), dict):
            t = e["step_update"].get("usage") or {}
            if t:
                u["input"] += int(t.get("input_tokens") or 0)
                u["cached"] += int(t.get("cache_read_tokens") or 0)
                u["output"] += int(t.get("output_tokens") or 0) + int(t.get("thinking_tokens") or 0)
                u["reasoning"] += int(t.get("thinking_tokens") or 0)
                u["calls"] += 1
        elif backend == "claude-code" and e.get("type") == "result":
            t = e.get("usage") or {}
            u["input"] += int(t.get("input_tokens") or 0) + int(t.get("cache_creation_input_tokens") or 0)
            u["cached"] += int(t.get("cache_read_input_tokens") or 0)
            u["output"] += int(t.get("output_tokens") or 0)
            u["calls"] += 1
            if e.get("total_cost_usd") is not None:
                cost += float(e["total_cost_usd"])
                has_cost = True
    if has_cost:
        u["reported_cost"] = round(cost, 6)
    return u


def run_started(run_dir: Path) -> Optional[float]:
    """Start of a porch run (epoch seconds), from its meta.json."""
    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        stamp = meta["started_at"].replace("Z", "+00:00")
        import datetime as _dt
        return _dt.datetime.fromisoformat(stamp).timestamp()
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def duration_seconds(raw: Path, started: Optional[float]) -> Optional[int]:
    """Wall time of one agent: porch starts all agents together and writes each
    raw stream when that agent finishes, so its last write ends the run."""
    if started is None:
        return None
    try:
        return max(int(raw.stat().st_mtime - started), 0)
    except OSError:
        return None


# ------------------------------------------------------------------ prices

def load_catalogue(refresh: bool = False) -> Dict[str, Dict[str, float]]:
    """OpenRouter list prices as USD per million tokens, keyed by model id."""
    cache = _home() / "cache" / "openrouter-models.json"
    data = None
    if not refresh and cache.is_file() and time.time() - cache.stat().st_mtime < CATALOGUE_MAX_AGE:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
    if data is None:
        try:
            with urllib.request.urlopen(CATALOGUE_URL, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            if cache.is_file():  # stale beats nothing
                try:
                    data = json.loads(cache.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = None
    prices: Dict[str, Dict[str, float]] = {}
    for m in (data or {}).get("data", []):
        p = m.get("pricing") or {}
        try:
            inp = float(p.get("prompt") or 0) * 1e6
            out = float(p.get("completion") or 0) * 1e6
            cached = float(p.get("input_cache_read") or p.get("prompt") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        prices[m.get("id", "")] = {"input": inp, "cached": cached, "output": out}
    return prices


def _strip_effort(model: str) -> str:
    for suffix in EFFORT_SUFFIXES:
        if model.endswith(suffix):
            return model[: -len(suffix)]
    return model


def _match_by_name(name: str, catalogue: Optional[Dict[str, Dict[str, float]]]) -> Optional[str]:
    """OpenRouter id whose model part is `name`, or `name` plus a date suffix
    (`qwen3.8-max` -> `qwen/qwen3.8-max-0902`). Used for OpenCode's own
    providers (Go, Zen), whose ids carry no vendor."""
    if not catalogue:
        return None
    exact = [cid for cid in catalogue if cid.split("/", 1)[-1] == name]
    if exact:
        return sorted(exact, key=len)[0]
    dated = [cid for cid in catalogue
             if re.fullmatch(re.escape(name) + r"-\d{4}", cid.split("/", 1)[-1])]
    return sorted(dated)[-1] if dated else None


def catalogue_id(backend: str, model: str, pricing: Dict[str, Any],
                 catalogue: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[str]:
    """Best guess of the OpenRouter id for a porch agent's model."""
    mapped = (pricing.get("models") or {}).get(model)
    if mapped:
        return mapped
    if backend == "opencode":
        provider, _, rest = model.partition("/")
        if provider == "openrouter":
            return rest
        if provider in OPENCODE_OWN_PROVIDERS:
            return _match_by_name(rest, catalogue)
        return model if rest else None
    vendor = BACKEND_VENDOR.get(backend)
    if not vendor:
        return None
    return f"{vendor}/{_strip_effort(model)}"


def price_for(backend: str, model: str, pricing: Dict[str, Any],
              catalogue: Dict[str, Dict[str, float]]) -> Tuple[Optional[Dict[str, float]], str]:
    explicit = (pricing.get("prices") or {}).get(model)
    if explicit:
        return explicit, "pricing.json"
    cid = catalogue_id(backend, model, pricing, catalogue)
    if cid and cid in catalogue:
        return catalogue[cid], f"openrouter:{cid}"
    return None, "unpriced"


def billing_mode(agent_id: str, backend: str, model: str, pricing: Dict[str, Any]) -> str:
    table = dict(DEFAULT_BILLING)
    table.update(pricing.get("billing") or {})
    if agent_id in table:
        return table[agent_id]
    if backend == "opencode" and model.startswith("opencode-go/"):
        return "subscription"
    return table.get(backend, "metered")


def cost_row(agent_id: str, backend: str, model: str, raw: Path,
             pricing: Dict[str, Any], catalogue: Dict[str, Dict[str, float]],
             started: Optional[float] = None) -> Dict[str, Any]:
    u = extract_usage(raw, backend) if raw.is_file() else empty_usage()
    price, source = price_for(backend, model, pricing, catalogue)
    billing = billing_mode(agent_id, backend, model, pricing)
    # A plan-covered OpenCode Go model reports cost 0; show its list-price
    # weight instead, like Codex, so plan peers stay comparable.
    plan_zero = (billing == "subscription" and u["reported_cost"] == 0
                 and (u["input"] or u["cached"] or u["output"]))
    if u["reported_cost"] is not None and not plan_zero:
        cost, source = u["reported_cost"], "reported by backend"
    elif price:
        cost = round((u["input"] * price["input"] + u["cached"] * price["cached"]
                      + u["output"] * price["output"]) / 1e6, 6)
    else:
        cost = None
    return {
        "agent": agent_id, "backend": backend, "model": model,
        "billing": billing_mode(agent_id, backend, model, pricing),
        "tokens": {k: u[k] for k in ("input", "cached", "output", "reasoning")},
        "calls": u["calls"], "cost_usd": cost, "price_source": source,
        "price_per_mtok": price, "duration_s": duration_seconds(raw, started) if raw.is_file() else None,
        "raw": str(raw) if raw.is_file() else None,
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def total(pred) -> float:
        return round(sum(r["cost_usd"] or 0 for r in rows if pred(r)), 4)

    return {
        "metered_usd": total(lambda r: r["billing"] == "metered"),
        "subscription_equiv_usd": total(lambda r: r["billing"] == "subscription"),
        "all_list_price_usd": total(lambda r: True),
        "unpriced_agents": [r["agent"] for r in rows if r["cost_usd"] is None],
    }


def fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"${v:.2f}" if v >= 0.995 else f"${v:.3f}"


def fmt_tokens(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def table_md(rows: List[Dict[str, Any]]) -> str:
    lines = ["| Peer | Model | Billing | Input | Cached | Output | Time | Cost | Price source |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        t = r["tokens"]
        dur = f"{r['duration_s'] // 60}m{r['duration_s'] % 60:02d}s" if r.get("duration_s") is not None else "?"
        cost = fmt_usd(r["cost_usd"]) + (" (plan)" if r["billing"] == "subscription" and r["cost_usd"] is not None else "")
        lines.append(f"| {r['agent']} | `{r['model']}` | {r['billing']} | {fmt_tokens(t['input'])} | "
                     f"{fmt_tokens(t['cached'])} | {fmt_tokens(t['output'])} | {dur} | {cost} | {r['price_source']} |")
    s = summarize(rows)
    lines += ["", f"**Spent (metered):** {fmt_usd(s['metered_usd'])} · "
                  f"**Covered by subscriptions (list-price equivalent):** {fmt_usd(s['subscription_equiv_usd'])} · "
                  f"**All at list price:** {fmt_usd(s['all_list_price_usd'])}"]
    if s["unpriced_agents"]:
        lines.append(f"\nUnpriced: {', '.join(s['unpriced_agents'])} (add them to pricing.json).")
    return "\n".join(lines)
