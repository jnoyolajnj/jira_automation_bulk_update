"""
Jira User Story checklist automation.

Reads an Excel file listing user stories (one sheet per team, or a Team column),
fetches each issue from Jira, runs the checklist verifications from
"TQ checklist Jira US ready check", optionally auto-updates fields or emails
the team leader, and writes a report with PASS/FAIL, failing steps, execution
time and runtime errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
from openpyxl import Workbook, load_workbook

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("jira_checker")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    name: str
    passed: bool
    message: str = ""
    action: str = ""  # "updated", "emailed", "skipped", "none"
    error: str = ""


@dataclass
class IssueResult:
    issue_key: str
    team: str
    overall: str = "PASS"                 # PASS or FAIL
    duration_seconds: float = 0.0
    runtime_error: str = ""
    steps: list[StepResult] = field(default_factory=list)

    def failing_step_names(self) -> list[str]:
        return [s.name for s in self.steps if not s.passed]


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Jira client
# ---------------------------------------------------------------------------

class JiraClient:
    def __init__(self, base_url: str, bearer_token: str, verify_ssl: bool = True, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.session.verify = verify_ssl
        self.timeout = timeout

    def get_issue(self, key: str) -> dict[str, Any]:
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        payload = {"fields": fields}
        r = self.session.put(url, json=payload, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Jira update failed ({r.status_code}): {r.text}")


# ---------------------------------------------------------------------------
# Emailer
# ---------------------------------------------------------------------------

class Mailer:
    def __init__(self, smtp_cfg: dict[str, Any], dry_run: bool = False):
        self.cfg = smtp_cfg
        self.dry_run = dry_run

    def send(self, to_address: str, subject: str, body: str) -> None:
        if self.dry_run or not to_address:
            log.info("[DRY-RUN email] to=%s subject=%s", to_address, subject)
            return
        msg = EmailMessage()
        msg["From"] = self.cfg["from_address"]
        msg["To"] = to_address
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(self.cfg["host"], self.cfg["port"]) as s:
            if self.cfg.get("use_tls"):
                s.starttls()
            user = self.cfg.get("username") or os.getenv("SMTP_USERNAME")
            pwd = self.cfg.get("password") or os.getenv("SMTP_PASSWORD")
            if user and pwd:
                s.login(user, pwd)
            s.send_message(msg)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _list_values(items: list[dict] | None, key: str = "name") -> list[str]:
    if not items:
        return []
    return [i.get(key, "") for i in items]


def check_contains_named(item_list: list[dict] | None, expected_name: str) -> bool:
    return expected_name in _list_values(item_list or [], "name")


class IssueProcessor:
    def __init__(self, jira: JiraClient, mailer: Mailer, config: dict[str, Any],
                 auto_update: bool = True, send_emails: bool = True):
        self.jira = jira
        self.mailer = mailer
        self.cfg = config
        self.auto_update = auto_update
        self.send_emails = send_emails
        self.field_ids = config["field_ids"]
        self.expected = config["expected_values"]
        self.teams = config["teams"]

    # --- helpers -----------------------------------------------------------

    def _email_leader(self, team: str, subject: str, body: str, result: StepResult) -> None:
        team_info = self.teams.get(team)
        if not team_info or not team_info.get("leader_email"):
            result.action = "email-skipped"
            result.error = f"No leader email configured for team '{team}'"
            return
        if not self.send_emails:
            result.action = "email-skipped (disabled)"
            return
        try:
            self.mailer.send(team_info["leader_email"], subject, body)
            result.action = f"emailed:{team_info['leader_email']}"
        except Exception as e:  # noqa: BLE001
            result.action = "email-failed"
            result.error = str(e)

    def _update(self, key: str, fields: dict[str, Any], result: StepResult) -> None:
        if not self.auto_update:
            result.action = "update-skipped (disabled)"
            return
        try:
            self.jira.update_issue(key, fields)
            result.action = f"updated:{list(fields.keys())}"
        except Exception as e:  # noqa: BLE001
            result.action = "update-failed"
            result.error = str(e)

    # --- check methods ------------------------------------------------------

    def check_affects_version(self, key: str, fields: dict) -> StepResult:
        expected = self.expected["affects_version"]
        current = _list_values(fields.get("versions"), "name")
        if expected in current:
            return StepResult("Affects version", True, f"Contains '{expected}'")
        res = StepResult("Affects version", False, f"Missing '{expected}' (current={current})")
        new_versions = [{"name": v} for v in current] + [{"name": expected}]
        self._update(key, {"versions": new_versions}, res)
        return res

    def check_fix_version(self, key: str, fields: dict) -> StepResult:
        expected = self.expected["fix_version"]
        current = _list_values(fields.get("fixVersions"), "name")
        if expected in current:
            return StepResult("Fix version", True, f"Contains '{expected}'")
        res = StepResult("Fix version", False, f"Missing '{expected}' (current={current})")
        new_versions = [{"name": v} for v in current] + [{"name": expected}]
        self._update(key, {"fixVersions": new_versions}, res)
        return res

    def check_component(self, key: str, fields: dict) -> StepResult:
        expected = self.expected["component"]
        current = _list_values(fields.get("components"), "name")
        if expected in current:
            return StepResult("Component", True, f"Contains '{expected}'")
        res = StepResult("Component", False, f"Missing '{expected}' (current={current})")
        new_components = [{"name": c} for c in current] + [{"name": expected}]
        self._update(key, {"components": new_components}, res)
        return res

    def check_labels(self, key: str, fields: dict, team: str) -> StepResult:
        base_label = self.expected["base_label"]
        current_labels = fields.get("labels") or []
        missing: list[str] = []
        if base_label not in current_labels:
            missing.append(base_label)

        team_info = self.teams.get(team)
        team_label = team_info.get("label") if team_info else None
        if team_label and team_label not in current_labels:
            missing.append(team_label)

        if not missing:
            return StepResult("Labels", True, f"Has {base_label}" + (f" + {team_label}" if team_label else ""))

        res = StepResult("Labels", False, f"Missing labels: {missing}")
        new_labels = current_labels + missing
        self._update(key, {"labels": new_labels}, res)
        return res

    def check_acceptance_criteria(self, key: str, fields: dict, team: str) -> StepResult:
        field_id = self.field_ids["acceptance_criteria"]
        body = fields.get(field_id) or ""
        keywords = self.cfg["acceptance_criteria_keywords"]
        missing = [kw for kw in keywords if not re.search(rf"\b{re.escape(kw)}\b", body, re.IGNORECASE)]
        if body.strip() and not missing:
            return StepResult("Acceptance Criteria", True, f"Contains {keywords}")
        msg = "Empty" if not body.strip() else f"Missing parts: {missing}"
        res = StepResult("Acceptance Criteria", False, msg)
        self._email_leader(
            team,
            subject=f"[Jira Check] {key} - Acceptance Criteria incomplete",
            body=f"User Story {key} has an incomplete Acceptance Criteria.\n{msg}\n",
            result=res,
        )
        return res

    def check_compliance_type(self, key: str, fields: dict) -> StepResult:
        field_id = self.field_ids["compliance_type"]
        v = fields.get(field_id)
        if _has_value(v):
            return StepResult("Compliance type", True, "Filled")
        res = StepResult("Compliance type", False, "Empty")
        self._update(key, {field_id: [{"value": self.expected["compliance_type"]}]}, res)
        return res

    def check_epic_link(self, key: str, fields: dict, team: str) -> StepResult:
        field_id = self.field_ids["epic_link"]
        v = fields.get(field_id)
        if _has_value(v):
            return StepResult("Epic Link", True, f"Filled={v}")
        res = StepResult("Epic Link", False, "Empty or missing")
        self._email_leader(
            team,
            subject=f"[Jira Check] {key} - Epic Link missing",
            body=f"User Story {key} has no Epic Link. Please review.\n",
            result=res,
        )
        return res

    def check_team_name(self, key: str, fields: dict) -> StepResult:
        field_id = self.field_ids["team_name"]
        v = fields.get(field_id)
        if _has_value(v):
            return StepResult("Team Name", True, "Filled")
        res = StepResult("Team Name", False, "Empty")
        self._update(key, {field_id: [{"value": self.expected["team_name"]}]}, res)
        return res

    def check_requirement_id(self, key: str, fields: dict, team: str) -> StepResult:
        field_id = self.field_ids["requirement_id"]
        v = fields.get(field_id)
        if _has_value(v):
            return StepResult("Requirement ID", True, f"Filled={v}")
        res = StepResult("Requirement ID", False, "Empty or missing")
        self._email_leader(
            team,
            subject=f"[Jira Check] {key} - Requirement ID missing",
            body=f"User Story {key} has no Requirement ID. Please review.\n",
            result=res,
        )
        return res

    def check_region(self, key: str, fields: dict) -> StepResult:
        field_id = self.field_ids["region"]
        v = fields.get(field_id)
        if _has_value(v):
            return StepResult("Region", True, "Filled")
        res = StepResult("Region", False, "Empty")
        self._update(key, {field_id: [{"value": self.expected["region"]}]}, res)
        return res

    def check_sector(self, key: str, fields: dict) -> StepResult:
        field_id = self.field_ids["sector"]
        v = fields.get(field_id)
        if _has_value(v):
            return StepResult("Sector", True, "Filled")
        res = StepResult("Sector", False, "Empty")
        self._update(key, {field_id: [{"value": self.expected["sector"]}]}, res)
        return res

    def check_description(self, key: str, fields: dict, team: str) -> StepResult:
        body = fields.get("description") or ""
        keywords = self.cfg["description_keywords"]
        missing = [kw for kw in keywords if not re.search(rf"\b{re.escape(kw)}\b", body, re.IGNORECASE)]
        if body.strip() and not missing:
            return StepResult("Description", True, f"Contains {keywords}")
        msg = "Empty" if not body.strip() else f"Missing parts: {missing}"
        res = StepResult("Description", False, msg)
        self._email_leader(
            team,
            subject=f"[Jira Check] {key} - Description incomplete",
            body=f"User Story {key} has an incomplete Description.\n{msg}\n",
            result=res,
        )
        return res

    def check_issue_links(self, key: str, fields: dict, team: str) -> StepResult:
        """'is child task of' target should match the Requirement ID / parent URL."""
        req_id_raw = fields.get(self.field_ids["requirement_id"])
        req_id = str(req_id_raw).strip() if req_id_raw is not None else ""

        parent_url = fields.get("customfield_10700") or ""
        parent_key_from_url = ""
        m = re.search(r"/browse/([A-Z]+-\d+)", parent_url)
        if m:
            parent_key_from_url = m.group(1)

        child_of_keys: list[str] = []
        for link in fields.get("issuelinks") or []:
            link_type = (link.get("type") or {}).get("inward", "")
            if link_type.lower() == "is child task of" and link.get("inwardIssue"):
                child_of_keys.append(link["inwardIssue"]["key"])

        if not child_of_keys:
            res = StepResult("Issue Links", False, "No 'is child task of' link found")
            self._email_leader(
                team,
                subject=f"[Jira Check] {key} - Missing child-of link",
                body=f"User Story {key} has no 'is child task of' issue link.\n",
                result=res,
            )
            return res

        # Discrepancy check: child-of target must appear either in parent URL
        # (customfield_10700) or match the Requirement ID value.
        candidates = {parent_key_from_url, req_id}
        candidates.discard("")
        matched = any(ck in candidates for ck in child_of_keys) if candidates else False

        if matched:
            return StepResult("Issue Links", True, f"Child-of {child_of_keys} matches requirement")

        msg = (
            f"Child-of={child_of_keys}, RequirementID={req_id!r}, "
            f"ParentKeyFromURL={parent_key_from_url!r}"
        )
        res = StepResult("Issue Links", False, f"Discrepancy: {msg}")
        self._email_leader(
            team,
            subject=f"[Jira Check] {key} - Issue Link / Requirement ID discrepancy",
            body=f"User Story {key} has a discrepancy between issue links and Requirement ID.\n{msg}\n",
            result=res,
        )
        return res

    # --- orchestration ------------------------------------------------------

    def process(self, issue_key: str, team: str) -> IssueResult:
        started = time.perf_counter()
        result = IssueResult(issue_key=issue_key, team=team)

        try:
            issue = self.jira.get_issue(issue_key)
        except Exception as e:  # noqa: BLE001
            result.runtime_error = f"GET issue failed: {e}"
            result.overall = "FAIL"
            result.duration_seconds = round(time.perf_counter() - started, 3)
            return result

        fields = issue.get("fields", {})

        steps = [
            self.check_affects_version(issue_key, fields),
            self.check_fix_version(issue_key, fields),
            self.check_component(issue_key, fields),
            self.check_labels(issue_key, fields, team),
            self.check_acceptance_criteria(issue_key, fields, team),
            self.check_compliance_type(issue_key, fields),
            self.check_epic_link(issue_key, fields, team),
            self.check_team_name(issue_key, fields),
            self.check_requirement_id(issue_key, fields, team),
            self.check_region(issue_key, fields),
            self.check_sector(issue_key, fields),
            self.check_description(issue_key, fields, team),
            self.check_issue_links(issue_key, fields, team),
        ]
        result.steps = steps
        result.overall = "PASS" if all(s.passed for s in steps) else "FAIL"
        result.duration_seconds = round(time.perf_counter() - started, 3)
        return result


# ---------------------------------------------------------------------------
# Excel I/O
# ---------------------------------------------------------------------------

def read_input(path: Path) -> list[tuple[str, str]]:
    """Return a list of (issue_key, team). Team is the sheet name unless a
    'Team' column overrides it."""
    wb = load_workbook(path, read_only=True, data_only=True)
    rows: list[tuple[str, str]] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        header = [str(c.value).strip() if c.value is not None else "" for c in next(ws.iter_rows(max_row=1))]
        try:
            key_idx = next(i for i, h in enumerate(header) if h.lower() in ("issuekey", "issue key", "key"))
        except StopIteration:
            log.warning("Sheet %r has no IssueKey column; skipping", sheet_name)
            continue
        team_idx = next((i for i, h in enumerate(header) if h.lower() == "team"), None)

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[key_idx] is None:
                continue
            key = str(row[key_idx]).strip()
            if not key:
                continue
            team = sheet_name
            if team_idx is not None and row[team_idx]:
                team = str(row[team_idx]).strip()
            rows.append((key, team))
    return rows


def write_report(results: list[IssueResult], output_path: Path) -> None:
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append([
        "Issue Key", "Team", "Overall", "Duration (s)", "Failing Steps",
        "Runtime Error",
    ])
    for r in results:
        summary.append([
            r.issue_key,
            r.team,
            r.overall,
            r.duration_seconds,
            "; ".join(r.failing_step_names()),
            r.runtime_error,
        ])

    details = wb.create_sheet("Details")
    details.append([
        "Issue Key", "Team", "Step", "Passed", "Message", "Action", "Error",
    ])
    for r in results:
        if r.runtime_error and not r.steps:
            details.append([r.issue_key, r.team, "-", False, "-", "-", r.runtime_error])
            continue
        for s in r.steps:
            details.append([
                r.issue_key, r.team, s.name, "PASS" if s.passed else "FAIL",
                s.message, s.action, s.error,
            ])

    wb.save(output_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jira US ready-check automation")
    parser.add_argument("--input", "-i", required=True, help="Input Excel file with user stories")
    parser.add_argument("--config", "-c", default="config.json", help="Path to config.json")
    parser.add_argument("--output", "-o", default=None, help="Output report path (default: report_<ts>.xlsx)")
    parser.add_argument("--no-update", action="store_true", help="Do not push field updates to Jira")
    parser.add_argument("--no-email", action="store_true", help="Do not send emails to leaders")
    parser.add_argument("--dry-run", action="store_true", help="Equivalent to --no-update --no-email")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    config_path = Path(args.config)
    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        return 2
    if not config_path.exists():
        log.error("Config file not found: %s", config_path)
        return 2

    config = load_config(config_path)
    token = os.getenv("JIRA_BEARER_TOKEN")
    if not token:
        log.error("JIRA_BEARER_TOKEN not set (env or .env file)")
        return 2

    auto_update = not (args.no_update or args.dry_run)
    send_emails = not (args.no_email or args.dry_run)

    jira = JiraClient(
        base_url=config["jira"]["base_url"],
        bearer_token=token,
        verify_ssl=config["jira"].get("verify_ssl", True),
        timeout=config["jira"].get("timeout_seconds", 30),
    )
    mailer = Mailer(config["smtp"], dry_run=not send_emails)
    processor = IssueProcessor(jira, mailer, config, auto_update=auto_update, send_emails=send_emails)

    stories = read_input(input_path)
    log.info("Processing %d user stories", len(stories))

    results: list[IssueResult] = []
    for idx, (key, team) in enumerate(stories, start=1):
        log.info("[%d/%d] %s (team=%s)", idx, len(stories), key, team)
        try:
            res = processor.process(key, team)
        except Exception:  # noqa: BLE001 - never let one issue stop the batch
            res = IssueResult(
                issue_key=key, team=team, overall="FAIL",
                runtime_error=traceback.format_exc(limit=3),
            )
        log.info("  -> %s in %.2fs (failing=%s)",
                 res.overall, res.duration_seconds, res.failing_step_names())
        results.append(res)

    out = Path(args.output) if args.output else Path(
        f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    write_report(results, out)
    log.info("Report written: %s", out)

    fails = sum(1 for r in results if r.overall != "PASS")
    log.info("Done. %d PASS, %d FAIL", len(results) - fails, fails)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
