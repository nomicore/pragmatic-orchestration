"""Findings, peer value and the cross-round scoreboard for peer-handoff.

A round record (written by `handoff report`) ends with a JSON block
`<!-- round-record ... -->`. The orchestrator adds two lists to it after
stitching the reviews together:

  "peer_runs": one entry per peer that was launched
      {"id": "codex", "agent": "codex", "model": "gpt-6.1-sol",
       "billing": "subscription", "cost_usd": 0.6, "duration_s": 336,
       "status": "report" | "partial" | "failed"}

  "findings": one entry per claim any peer made, including rejected ones
      {"id": 1, "title": "...",
       "raised_by": ["codex", "kimi"] or "all",
       "reproduced_by": ["codex"],
       "severity": "critical" | "major" | "minor" | "nit",
       "category": "security" | "correctness" | "reliability" | "performance"
                   | "ops" | "tests" | "docs" | "design",
       "verdict": "true" | "partly" | "false" | "unverified",
       "outcome": "fixed" | "accepted" | "deferred" | "open"
                  | "out-of-scope" | "not-a-defect" | "rejected",
       "note": "why it was used or discarded"}

Scoring (per finding, credited to each peer that raised it):

  value = severity weight x verdict factor x outcome factor
          x (1 / number of peers that raised it) x reproduction bonus

Shared findings are split: what every peer finds is worth little to any one
of them, because the cheapest peer would have found it too. A true finding the
orchestrator discards (out of scope, over-engineered, not a defect) earns
little or nothing, so a peer that keeps proposing work we never do scores low.
A false claim costs half its severity weight to each peer that made it, because
someone had to check it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SEVERITY_WEIGHT = {"critical": 8.0, "major": 4.0, "minor": 2.0, "nit": 1.0}
VERDICT_FACTOR = {"true": 1.0, "partly": 0.5, "unverified": 0.0, "false": 0.0}
OUTCOME_FACTOR = {"fixed": 1.0, "accepted": 1.0, "deferred": 0.6, "open": 0.6,
                  "out-of-scope": 0.25, "not-a-defect": 0.0, "rejected": 0.0}
CATEGORIES = ("security", "correctness", "reliability", "performance", "ops", "tests", "docs", "design")
STATUSES = ("report", "partial", "failed")
REPRO_BONUS = 1.25
FALSE_PENALTY = 0.5
ADOPTED = ("fixed", "accepted")
DISCARDED = ("out-of-scope", "not-a-defect", "rejected")

RECORD_RE = re.compile(r"<!-- round-record\s*(\{.*?\})\s*-->", re.S)
EFFORT_RE = re.compile(r"-(xhigh|high|medium|low|minimal)$")
DATE_RE = re.compile(r"-\d{4}$")


# ------------------------------------------------------------ record I/O

def read_record(path: Path) -> Tuple[str, Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    m = RECORD_RE.search(text)
    if not m:
        raise ValueError(f"{path}: no <!-- round-record --> block")
    return text, json.loads(m.group(1))


def write_record(path: Path, text: str, record: Dict[str, Any]) -> str:
    block = "<!-- round-record\n" + json.dumps(record, indent=2, ensure_ascii=False) + "\n-->"
    new = RECORD_RE.sub(lambda _m: block, text, count=1)
    path.write_text(new, encoding="utf-8")
    return new


def model_key(model: str) -> str:
    """One name per model across routes: `opencode-go/gemini-4-argon`,
    `gemini-4-argon-high` and `openrouter/google/gemini-4-argon` all give
    `gemini-4-argon`; `qwen3.8-max-0902` gives `qwen3.8-max`."""
    name = (model or "?").rsplit("/", 1)[-1]
    return DATE_RE.sub("", EFFORT_RE.sub("", name))


def _usd(cell: str) -> Optional[float]:
    m = re.search(r"\$([0-9.]+)", cell)
    return float(m.group(1)) if m else None


def _secs(cell: str) -> Optional[int]:
    m = re.match(r"\s*(\d+)m(\d+)s", cell)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def runs_from_table(text: str) -> List[Dict[str, Any]]:
    """Fallback for records without `peer_runs`: parse the cost table."""
    runs: List[Dict[str, Any]] = []
    section = text.split("## Peers and cost", 1)[-1].split("\n## ", 1)[0]
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or cells[0] in ("Peer", "") or set(cells[0]) <= {"-"}:
            continue
        runs.append({"id": cells[0], "agent": cells[0], "model": cells[1].strip("`"),
                     "billing": cells[2], "cost_usd": _usd(cells[7]),
                     "duration_s": _secs(cells[6]), "status": "report"})
    return runs


def peer_runs(record: Dict[str, Any], text: str = "") -> List[Dict[str, Any]]:
    runs = record.get("peer_runs") or (runs_from_table(text) if text else [])
    for r in runs:
        r.setdefault("id", r.get("agent"))
        r.setdefault("status", "report")
    return runs


# ------------------------------------------------------------ validation

def validate(record: Dict[str, Any], text: str = "") -> List[str]:
    problems: List[str] = []
    runs = peer_runs(record, text)
    ids = [r["id"] for r in runs]
    if len(set(ids)) != len(ids):
        problems.append(f"peer_runs: duplicate ids {ids}")
    for r in runs:
        if r["status"] not in STATUSES:
            problems.append(f"peer_runs {r['id']}: status {r['status']!r} not in {STATUSES}")
    for f in record.get("findings") or []:
        tag = f"finding {f.get('id', '?')}"
        for field, allowed in (("severity", SEVERITY_WEIGHT), ("verdict", VERDICT_FACTOR),
                               ("outcome", OUTCOME_FACTOR), ("category", CATEGORIES)):
            if f.get(field) not in allowed:
                problems.append(f"{tag}: {field} {f.get(field)!r} not in {sorted(allowed)}")
        raised = f.get("raised_by")
        if raised != "all":
            if not raised:
                problems.append(f"{tag}: raised_by is empty")
            for pid in raised or []:
                if pid not in ids:
                    problems.append(f"{tag}: raised_by {pid!r} is not a peer_runs id {ids}")
        for pid in f.get("reproduced_by") or []:
            if raised != "all" and pid not in (raised or []):
                problems.append(f"{tag}: reproduced_by {pid!r} did not raise it")
    return problems


# ------------------------------------------------------------ scoring

def raisers(f: Dict[str, Any], runs: List[Dict[str, Any]]) -> List[str]:
    if f.get("raised_by") == "all":
        return [r["id"] for r in runs if r["status"] != "failed"]
    return list(f.get("raised_by") or [])


def finding_value(f: Dict[str, Any], pid: str, n: int) -> float:
    w = SEVERITY_WEIGHT.get(f.get("severity"), 0.0)
    if f.get("verdict") == "false":
        return -FALSE_PENALTY * w
    v = w * VERDICT_FACTOR.get(f.get("verdict"), 0.0) * OUTCOME_FACTOR.get(f.get("outcome"), 0.0)
    v /= max(n, 1)
    if pid in (f.get("reproduced_by") or []):
        v *= REPRO_BONUS
    return v


def empty_stats() -> Dict[str, Any]:
    return {"rounds": 0, "failed": 0, "spent_usd": 0.0, "plan_usd": 0.0, "minutes": 0.0,
            "raised": 0, "real": 0, "unique": 0, "false": 0, "security": 0,
            "adopted": 0, "discarded": 0, "score": 0.0, "routes": set()}


def round_scores(record: Dict[str, Any], text: str = "") -> Dict[str, Dict[str, Any]]:
    """Per peer run id: this round's stats."""
    runs = peer_runs(record, text)
    out: Dict[str, Dict[str, Any]] = {}
    for r in runs:
        s = empty_stats()
        s["rounds"] = 1
        s["failed"] = 1 if r["status"] == "failed" else 0
        cost = r.get("cost_usd") or 0.0
        s["spent_usd" if r.get("billing") != "subscription" else "plan_usd"] = cost
        s["minutes"] = (r.get("duration_s") or 0) / 60
        s["routes"] = {r.get("agent") or r["id"]}
        s["model"] = r.get("model", "?")
        out[r["id"]] = s
    for f in record.get("findings") or []:
        who = [p for p in raisers(f, runs) if p in out]
        real = f.get("verdict") in ("true", "partly")
        for pid in who:
            s = out[pid]
            s["raised"] += 1
            s["score"] += finding_value(f, pid, len(who))
            if f.get("verdict") == "false":
                s["false"] += 1
            if real:
                s["real"] += 1
                s["unique"] += 1 if len(who) == 1 else 0
                s["security"] += 1 if f.get("category") == "security" else 0
                s["adopted"] += 1 if f.get("outcome") in ADOPTED else 0
                s["discarded"] += 1 if f.get("outcome") in DISCARDED else 0
    return out


def collect(paths: Iterable[Path]) -> List[Tuple[Path, str, Dict[str, Any]]]:
    files: List[Path] = []
    for p in paths:
        p = p.expanduser()
        if p.is_dir():
            files += sorted(p.rglob("*.md"))
        elif p.is_file():
            files.append(p)
    out = []
    seen = set()
    for f in files:
        rp = f.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        try:
            text, rec = read_record(f)
        except (ValueError, json.JSONDecodeError):
            continue
        out.append((f, text, rec))
    return sorted(out, key=lambda t: (t[2].get("date", ""), str(t[0])))


def scoreboard(records: List[Tuple[Path, str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    board: Dict[str, Dict[str, Any]] = {}
    for _path, text, rec in records:
        if not isinstance(rec.get("findings"), list):
            continue  # not yet scored: cost alone would skew value per dollar
        for _pid, s in round_scores(rec, text).items():
            key = model_key(s.pop("model"))
            b = board.setdefault(key, empty_stats())
            for k, v in s.items():
                if k == "routes":
                    b["routes"] |= v
                else:
                    b[k] += v
    return board


# ------------------------------------------------------------ rendering

def _f(v: float, digits: int = 1) -> str:
    return f"{v:.{digits}f}"


def _money(v: float) -> str:
    return f"${v:.2f}"


def board_md(board: Dict[str, Dict[str, Any]], scored: int, total: int) -> str:
    lines = [f"Rounds with findings recorded: {scored} of {total}.", "",
             "| Model | Routes | Rounds | Failed | Spent | Plan equiv. | Raised | Real | Unique | "
             "False | Security | Adopted | Discarded | Score | Score/round | List $ per point |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    order = sorted(board.items(), key=lambda kv: -kv[1]["score"] / max(kv[1]["rounds"], 1))
    for key, b in order:
        cost = b["spent_usd"] + b["plan_usd"]
        per_point = _money(cost / b["score"]) if b["score"] > 0 else "-"
        lines.append(
            f"| {key} | {', '.join(sorted(b['routes']))} | {b['rounds']} | {b['failed']} | "
            f"{_money(b['spent_usd'])} | {_money(b['plan_usd'])} | {b['raised']} | {b['real']} | "
            f"{b['unique']} | {b['false']} | {b['security']} | {b['adopted']} | {b['discarded']} | "
            f"{_f(b['score'])} | {_f(b['score'] / max(b['rounds'], 1))} | {per_point} |")
    lines += ["", "*Real*: verified true or partly true. *Unique*: real and raised by no other peer. "
              "*Adopted*: fixed or accepted. *Discarded*: real but out of scope, not a defect or rejected. "
              "*Score*: severity x verdict x outcome, split among the peers that raised it, x1.25 when the "
              "peer reproduced it; a false claim costs half its severity weight. *List $ per point*: "
              "spent plus plan equivalent, divided by score."]
    return "\n".join(lines)


def round_md(scores: Dict[str, Dict[str, Any]]) -> str:
    lines = ["| Peer | Status | Raised | Real | Unique | False | Security | Adopted | Discarded | Score |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for pid, s in sorted(scores.items(), key=lambda kv: -kv[1]["score"]):
        status = "failed" if s["failed"] else "report"
        lines.append(f"| {pid} | {status} | {s['raised']} | {s['real']} | {s['unique']} | {s['false']} | "
                     f"{s['security']} | {s['adopted']} | {s['discarded']} | {_f(s['score'])} |")
    return "\n".join(lines)


SCORE_START, SCORE_END = "<!-- peer-score -->", "<!-- /peer-score -->"


def put_round_score(text: str, scores: Dict[str, Dict[str, Any]]) -> str:
    """Insert or refresh the generated per-peer score table before `## Peer value`."""
    block = f"{SCORE_START}\n## Peer score\n\n{round_md(scores)}\n\n_Generated by `handoff findings` from the JSON block._\n{SCORE_END}"
    if SCORE_START in text and SCORE_END in text:
        head, rest = text.split(SCORE_START, 1)
        return head + block + rest.split(SCORE_END, 1)[1]
    if "\n## Peer value" in text:
        head, rest = text.split("\n## Peer value", 1)
        return head.rstrip() + "\n\n" + block + "\n\n## Peer value" + rest
    return text.rstrip() + "\n\n" + block + "\n"
