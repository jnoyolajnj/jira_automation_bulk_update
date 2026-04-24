# Jira User Story Ready-Check Automation

Automates the "TQ checklist Jira US ready check" for a list of user stories:
fetches each issue from Jira, verifies the 13 required fields, auto-updates
the ones the checklist allows and emails the team leader for those it
doesn't, then writes an Excel report with PASS/FAIL, failing steps, duration
and runtime errors.

## Files

| File | Purpose |
|------|---------|
| [jira_checker.py](jira_checker.py) | Main script |
| [config.json](config.json) | Field IDs, expected values, team → label/leader-email mappings |
| [create_sample_input.py](create_sample_input.py) | Builds a blank `input_user_stories.xlsx` template |
| [.env.example](.env.example) | Template for the bearer token and SMTP credentials |
| [requirements.txt](requirements.txt) | Python dependencies |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and paste your Jira Personal Access Token into JIRA_BEARER_TOKEN
```

Review [config.json](config.json) and adjust:
- `teams` — label codes and leader emails for Finance / Delivery / Loaner
- `field_ids` — the Jira custom field IDs used at your instance
- `smtp` — mail server host/port/credentials

## Input file

An Excel workbook with one sheet per team (`Finance`, `Delivery`, `Loaner`).
Each sheet must have at least an `IssueKey` column; the sheet name is used as
the team, or a `Team` column can override it per row.

Example:

| IssueKey | Team |
|----------|------|
| AASQ-72454 | Finance |

Generate a blank template with `python create_sample_input.py`.

## Run

```bash
python jira_checker.py -i input_user_stories.xlsx
```

Useful flags:
- `--dry-run` — no Jira writes, no emails sent (safe first pass)
- `--no-update` — verify but don't push field updates
- `--no-email` — verify but don't email leaders
- `-o report.xlsx` — custom report path (default: `report_<timestamp>.xlsx`)

## Report

The output Excel has two sheets:
- **Summary** — one row per issue with overall PASS/FAIL, duration, list of
  failing steps and runtime error (if any)
- **Details** — one row per (issue, step) with the individual message and
  action taken (`updated:...`, `emailed:...`, `update-failed`, etc.)

## Checks performed

1. Affects version contains `Osprey_2026` (auto-add)
2. Fix version contains `Osprey_2026` (auto-add)
3. Component contains `JDE` (auto-add)
4. Labels contain `Osprey` and the team label, e.g. `FIN` (auto-add)
5. Acceptance Criteria contains `Given`, `When`, `Then` (email leader)
6. Compliance type set, else fill with `GxP`
7. Epic Link set (email leader)
8. Team Name set, else fill with `EMEA_GMED_JDE8.12_JEDIKNIGHTS`
9. Requirement ID set (email leader)
10. Region set, else fill with `EMEA`
11. Sector set, else fill with `MedTech`
12. Description contains `Who`, `What`, `Why` (email leader)
13. Issue Links — "is child task of" matches Requirement ID / parent URL
    (email leader on discrepancy)
