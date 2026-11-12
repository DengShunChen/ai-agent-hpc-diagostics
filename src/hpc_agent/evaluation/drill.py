"""Synthetic drill kit: engineers analyse simulated incidents with raw files only, or with the agent's report too.

prepare: from a directory written by `hpc-agent simulate` (never its labels/), import the export, diagnose every
scenario, and write one packet per engineer in a crossover design (group 1: half A baseline, half B assisted;
group 2 the other way round), with a sheet to fill in. Nothing from labels/ is read or copied.

score: after the drill, compare the filled-in sheets with labels/ and write the pilot CSV that
`hpc-agent compare-pilot` reads, plus per-answer correctness. Time is reported together with the share of
correct answers per arm: a faster wrong answer is not an improvement.

A drill measures the difference on this simulator's scenarios, never MTTR on a real cluster
(docs/business-impact.md, evaluation/drill/README.md).
"""

from __future__ import annotations

import csv
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

from hpc_agent.data_adapter import ReadOnlyDataAdapter, default_data_root
from hpc_agent.ingest.deidentify import Pseudonymizer
from hpc_agent.ingest.importer import import_export
from hpc_agent.ingest.raw_export import RawExportAdapter
from hpc_agent.llm.provider import ChatLLM
from hpc_agent.runtime.definitions import load_definitions
from hpc_agent.simulate.scenario import Labels
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.cascade import run_cascade
from hpc_agent.workflows.diagnostic_workflow import render_report, run_diagnosis

SIMULATION_KEY = "simulated-scenarios-only-not-a-secret-0000"
SHEET_FIELDS = ("engineer", "scenario_id", "arm", "started_at", "answered_at", "answer_category", "answer_text",
                "confidence")
INSUFFICIENT = "insufficient_evidence"


class DrillError(ValueError):
    pass


def _engineer_codes(engineers: list[str]) -> list[str]:
    if len(engineers) < 2 or len(set(engineers)) != len(engineers):
        raise DrillError("give at least two distinct engineer codes (codes, not names)")
    for code in engineers:
        if not code.isalnum() or len(code) > 8:
            raise DrillError(f"engineer code must be short and alphanumeric (codes, not names): {code!r}")
    return engineers


def answer_choices(root: Path | None = None) -> list[str]:
    cats = sorted({c for d in load_definitions(root).values() if d.llm for c in d.llm.categories})
    return [*cats, INSUFFICIENT, "other"]


def prepare(directory: Path, out: Path, engineers: list[str], scopes: Mapping[str, frozenset[str]] | None = None,
            llm: ChatLLM | None = None, cascade: bool = False) -> dict:
    """Write out/root (imported data), out/reports, out/packets/<engineer>/ and out/assignment.csv."""
    engineers = _engineer_codes(engineers)
    export = directory / "export"
    if not export.is_dir():
        raise DrillError(f"no export/ in {directory} (expected a directory written by `hpc-agent simulate`)")
    if out.exists() and any(out.iterdir()):
        raise DrillError(f"output directory must be new or empty: {out}")
    if cascade and llm is None:
        raise DrillError("a cascade drill needs a model for the cases the rules hand over")
    _no_links(directory, export)
    ids = sorted(p.name for p in export.iterdir() if p.is_dir() and not p.is_symlink())
    if len(ids) < 2:
        raise DrillError("a drill needs at least two scenarios")
    files = {sid: _regular_files(export / sid) for sid in ids}  # checked before anything is written
    names = {sid: directory / "names" / f"{sid}.txt" for sid in ids}
    for path in names.values():
        if path.exists() or path.is_symlink():
            _no_links(directory, path)  # names/ itself, not only the file, must not be a link
            if not path.is_file():
                raise DrillError(f"names file must be a regular file: {path.name}")

    root = out / "root"
    shutil.copytree(default_data_root() / "knowledge", root / "knowledge")
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "DATA_CLASS.json").write_text(json.dumps({"class": "synthetic"}))
    report = import_export(RawExportAdapter(export), root, Pseudonymizer(SIMULATION_KEY))
    adapter = ReadOnlyDataAdapter(root)

    reports = out / "reports"
    reports.mkdir()
    for scenario_id in ids:
        if scenario_id in report.rejected:
            text = f"Agent report unavailable: import rejected ({report.rejected[scenario_id]})\n"
        else:
            try:
                incident = adapter.load_incident(scenario_id)
                run = (run_cascade(incident, build_default_gateway(adapter), llm, scopes) if cascade
                       else run_diagnosis(incident, build_default_gateway(adapter), scopes, llm=llm))
                text = render_report(run) + "\n"
            except Exception as error:  # noqa: BLE001 - the engineer gets no report, and the sheet says so
                text = f"Agent report unavailable: {type(error).__name__}\n"
        (reports / f"{scenario_id}.txt").write_text(text, encoding="utf-8")

    half = {sid: "A" if i % 2 == 0 else "B" for i, sid in enumerate(ids)}
    group = {code: 1 if i % 2 == 0 else 2 for i, code in enumerate(engineers)}
    choices = answer_choices()
    rows = []
    for code in engineers:
        packet = out / "packets" / code
        packet.mkdir(parents=True)
        (packet / "answer-choices.txt").write_text(
            "Answer with one of these categories (answer_category), and your reasoning in answer_text:\n"
            + "\n".join(f"- {c}" for c in choices) + "\n", encoding="utf-8")
        sheet = []
        for n, scenario_id in enumerate(ids, 1):
            assisted = (group[code] == 1) == (half[scenario_id] == "B")
            arm = "assisted" if assisted else "baseline"
            folder = packet / f"{n:02d}-{scenario_id}"
            _copy_files(export / scenario_id, files[scenario_id], folder / "raw")
            if assisted:
                shutil.copyfile(reports / f"{scenario_id}.txt", folder / "agent-report.txt")
                if names[scenario_id].is_file():  # pseudonym -> simulated name, to match the report to the raw files
                    shutil.copyfile(names[scenario_id], folder / "names.txt", follow_symlinks=False)
            sheet.append({"engineer": code, "scenario_id": scenario_id, "arm": arm})
            rows.append({"engineer": code, "group": group[code], "scenario_id": scenario_id,
                         "half": half[scenario_id], "arm": arm})
        _write_csv(packet / "sheet.csv", SHEET_FIELDS, sheet)
    _write_csv(out / "assignment.csv", ("engineer", "group", "scenario_id", "half", "arm"), rows)
    assert not any(p.is_symlink() for p in out.rglob("*")), "a drill packet must hold no links"
    return {"scenarios": len(ids), "engineers": len(engineers), "rejected": sorted(report.rejected)}


def _no_links(root: Path, path: Path) -> None:
    """No component from root (exclusive) down to path (inclusive) may be a link: a linked parent directory would
    make a plain-looking file resolve to labels/ or anywhere else."""
    rel = path.relative_to(root)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise DrillError(f"links are not allowed in a drill input ({current.relative_to(root)})")
    if not path.resolve().is_relative_to(root.resolve()):
        raise DrillError(f"drill input escapes its directory ({rel})")


def _regular_files(folder: Path) -> list[Path]:
    """Every file under a scenario's export folder, relative to it. Links and special files are refused: a link
    could point at labels/ or anywhere else, and a packet must only carry the export's own files."""
    out = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise DrillError(f"{folder.name}: links are not allowed in an export ({path.relative_to(folder)})")
        if path.is_file():
            out.append(path.relative_to(folder))
        elif not path.is_dir():
            raise DrillError(f"{folder.name}: not a regular file ({path.relative_to(folder)})")
    return out


def _copy_files(src: Path, files: list[Path], dst: Path) -> None:
    for rel in files:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target, follow_symlinks=False)


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


@dataclass(frozen=True)
class Answer:
    engineer: str
    scenario_id: str
    arm: str
    minutes: float | None  # None when the times are missing or invalid
    correct: bool | None  # None only when it cannot be judged (unknown scenario or arm); an empty answer is wrong
    answered: bool
    problems: tuple[str, ...] = ()


def score(sheets: list[Path], labels_dir: Path, out: Path) -> tuple[list[Answer], str]:
    """Write out/drill-results.csv (compare-pilot format) and out/drill-answers.csv; return answers and a summary.

    Correctness and time are judged separately: a wrong or missing answer stays in the correctness denominator
    even when its times are missing, and only rows with valid times go to the time comparison."""
    labels = {p.stem: Labels.model_validate_json(p.read_text(encoding="utf-8")) for p in labels_dir.glob("*.json")}
    answers, pilot_rows, answer_rows = [], [], []
    for sheet in sheets:
        with sheet.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                sid, arm, code = row.get("scenario_id", ""), row.get("arm", ""), row.get("engineer", "")
                label = labels.get(sid)
                start, end = _time(row.get("started_at") or ""), _time(row.get("answered_at") or "")
                answer = (row.get("answer_category") or "").strip()
                problems = []
                if label is None:
                    problems.append("unknown scenario")
                if arm not in ("baseline", "assisted"):
                    problems.append("arm must be baseline or assisted")
                if not answer:
                    problems.append("no answer")
                timed = bool(start and end) and end >= start
                if not (start and end):
                    problems.append("times missing or not ISO 8601 with a timezone")
                elif end < start:
                    problems.append("answered before it started")
                judged = label is not None and arm in ("baseline", "assisted")
                correct = None
                if judged:
                    correct = bool(answer) and (answer in label.leading_categories if label.leading_categories
                                                else answer == INSUFFICIENT)
                minutes = (end - start).total_seconds() / 60 if timed else None
                answers.append(Answer(code, sid, arm, minutes, correct, bool(answer), tuple(problems)))
                answer_rows.append({"engineer": code, "scenario_id": sid, "arm": arm, "answer_category": answer,
                                    "expected": " | ".join(label.leading_categories) if label else "",
                                    "correct": correct, "minutes": None if minutes is None else round(minutes, 1),
                                    "problems": "; ".join(problems)})
                if judged and timed:
                    category = label.leading_categories[0].split(".")[0] if label.leading_categories else "insufficient"
                    pilot_rows.append({"incident_id": f"{code}-{sid}", "site": "drill", "category": category,
                                       "period": arm, "detected_at": start.isoformat(),
                                       "localized_at": end.isoformat(), "resolved_at": ""})
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "drill-results.csv", ("incident_id", "site", "category", "period", "detected_at",
                                           "localized_at", "resolved_at"), pilot_rows)
    _write_csv(out / "drill-answers.csv", ("engineer", "scenario_id", "arm", "answer_category", "expected",
                                           "correct", "minutes", "problems"), answer_rows)
    return answers, summary(answers)


def summary(answers: list[Answer]) -> str:
    lines = ["# Synthetic drill result", "",
             "Simulated incidents with injected causes; **not** MTTR on a real cluster. Correctness counts every "
             "judged row (a missing answer is wrong, missing times do not remove a row); times count only rows with "
             "valid times.", "",
             "| Arm | Rows | Judged | Correct (of judged) | No answer | Valid times | Median minutes (valid times) "
             "| Median minutes (correct, valid times) |",
             "|---|---|---|---|---|---|---|---|"]

    def med(xs: list[Answer]) -> str:
        return f"{median(a.minutes for a in xs):.1f}" if xs else "–"

    for arm in ("baseline", "assisted"):
        rows = [a for a in answers if a.arm == arm]
        judged = [a for a in rows if a.correct is not None]
        right = [a for a in judged if a.correct]
        timed = [a for a in judged if a.minutes is not None]
        pct = f" ({100 * len(right) / len(judged):.0f}%)" if judged else ""
        lines.append(f"| {arm} | {len(rows)} | {len(judged)} | {len(right)}{pct} | {sum(not a.answered for a in rows)} "
                     f"| {len(timed)} | {med(timed)} | {med([a for a in timed if a.correct])} |")
    unjudged = [a for a in answers if a.correct is None]
    if unjudged:
        lines += ["", f"Rows that cannot be judged ({len(unjudged)}): "
                  + "; ".join(f"{a.engineer}/{a.scenario_id}: {', '.join(a.problems)}" for a in unjudged)]
    lines += ["", "Statistical comparison of times: `hpc-agent compare-pilot drill-results.csv` (valid times only)."]
    return "\n".join(lines) + "\n"
