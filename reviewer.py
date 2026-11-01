#!/usr/bin/env python3
"""Poll requested GitHub reviews; Codex reads snapshots, this process posts reviews."""
import argparse
from datetime import datetime, timedelta
import fcntl
import hashlib
import html
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import tarfile
import tempfile
import time
from zoneinfo import ZoneInfo

from submodule_context import expand_referenced_submodules as expand_context_submodules
from submodule_context import submodule_sources
from companion_context import collect_companion_context, resolve_companion_pull
from build_checks import run_checks, validation_summary, resolve_branch_revision
from review_policy import review_history, prepare_history, apply_policy, ledger_comment
from sms_notifier import SmsNotifier
from runtime_config import asset, load_config

ROOT = Path(os.environ.get("REVIEWER_DATA_DIR", str(Path(__file__).resolve().parent)))
CONFIG = {}
LOG = logging.getLogger("pr-reviewer")
MARKER = "<!-- codex-requested-pr-review:v1 -->"
ROUND_SIX_MARKER = "<!-- codex-requested-pr-round-six:v1 -->"
RESOLUTION_MARKER = "<!-- codex-requested-pr-resolution:v1 "
REVIEW_TIMEZONE = ZoneInfo("Africa/Dar_es_Salaam")
CONTEXT_POLICY_VERSION = 8


def review_model(now=None):
    timezone = ZoneInfo(CONFIG.get("schedule", {}).get("timezone", REVIEW_TIMEZONE.key))
    now = (now or datetime.now(timezone)).astimezone(timezone)
    profile = CONFIG.get("daytime_review", {}) if 11 <= now.hour < 17 else {}
    return profile.get("model", CONFIG["model"]), profile.get("reasoning", CONFIG["reasoning"])


def poll_window(now=None):
    schedule = CONFIG.get("schedule", {})
    timezone = ZoneInfo(schedule.get("timezone", REVIEW_TIMEZONE.key))
    now = (now or datetime.now(timezone)).astimezone(timezone)
    if "interval_minutes" in schedule:
        interval = schedule["interval_minutes"]
        if schedule.get("daytime_start_hour", 11) <= now.hour < schedule.get("daytime_end_hour", 17):
            interval = schedule.get("daytime_interval_minutes", interval)
        seconds = interval * 60
        start = datetime.fromtimestamp((int(now.timestamp()) // seconds) * seconds, timezone)
        return start, datetime.fromtimestamp(start.timestamp() + seconds, timezone)
    if 11 <= now.hour < 17:
        start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        return start, start + timedelta(minutes=30)
    start = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    return start, start + timedelta(minutes=10)


def claim_poll_window(*, path=None, log=None):
    start, next_start = poll_window()
    path = path or ROOT / "poll-schedule.json"
    log = log or LOG
    schedule = json.loads(path.read_text()) if path.exists() else {}
    if schedule.get("last_window") == start.isoformat():
        log.info("Schedule: next poll window starts %s", next_start.isoformat())
        return False
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"last_window": start.isoformat()}, indent=2) + "\n")
    temporary.replace(path)
    log.info("Schedule: poll window %s; next window %s", start.isoformat(), next_start.isoformat())
    return True


def command(args, *, input_text=None, cwd=None, timeout=120, env=None):
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=cwd, env=env,
                               start_new_session=True)
    try:
        out, err = process.communicate(
            input_text.encode() if input_text is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError(f"{Path(args[0]).name} timed out after {timeout}s") from None
    if process.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {err.decode(errors='replace')[-2000:]}")
    return out


def gh(*args, payload=None):
    return command([CONFIG["gh"], *args],
                   input_text=json.dumps(payload) if payload is not None else None)


def api(endpoint, *, payload=None):
    args = ["api", "--hostname", "github.com", endpoint]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    return json.loads(gh(*args, payload=payload))


def paginated(endpoint):
    pages = json.loads(gh("api", "--hostname", "github.com", "--paginate", "--slurp", endpoint))
    return [item for page in pages for item in page]


def save_state(state):
    temp = ROOT / "state.json.tmp"
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(ROOT / "state.json")


def same_user(left, right):
    return left.casefold() == right.casefold()


def repository_allowed(repo):
    return repo.casefold() in {name.casefold() for name in CONFIG.get("repositories", [])}


def eligible(pr, user, watched=False, *, include_draft=False):
    return (pr["state"] == "open" and (include_draft or not pr["draft"])
            and not same_user(pr["user"]["login"], user)
            and (watched or any(same_user(x["login"], user)
                                for x in pr["requested_reviewers"])))


def already_reviewed(reviews, user, sha):
    return any(same_user(r["user"]["login"], user) and r.get("commit_id") == sha
               and (r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
                    or (r["state"] == "COMMENTED" and MARKER in (r.get("body") or "")))
               for r in reviews)


def same_revision(before, after):
    return all(before[side]["sha"] == after[side]["sha"] for side in ("head", "base"))


def deferred_revision(pr, companions=(), dependencies=()):
    # A context-policy update retries a deferred revision once, without repeated model calls.
    policy = {"version": CONTEXT_POLICY_VERSION,
              "repositories": sorted(CONFIG.get("repositories", [])),
              "context_repositories": sorted(CONFIG.get("context_repositories", [])),
              "validation": CONFIG.get("validation", {}),
              "description": pr.get("body") or "", "title": pr.get("title") or "",
              "ai": CONFIG.get("ai", {}), "model": CONFIG.get("model"),
              "skills": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in CONFIG.get("skill_files", [])},
              "comments": {kind: sorted(
                  [[comment.get("id"), comment.get("updated_at"), comment.get("body")]
                   for comment in pr.get(kind, ())], key=lambda item: str(item[0]))
                  for kind in ("conversation_comments", "review_comments")}}
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    revision = [pr["head"]["sha"], pr["base"]["sha"], digest,
                sorted([item["repository"], item["number"], item["sha"]] for item in companions)]
    branches = sorted([item["repository"], item["branch"], item["sha"]]
                      for item in dependencies if item.get("branch"))
    if branches:
        revision.append(branches)
    return revision


def extract_snapshot(data, destination):
    """Extract regular files only; never follow archive links or write outside the snapshot."""
    omitted = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 30000 or sum(m.size for m in members) > 250_000_000:
            raise ValueError("Repository snapshot exceeds the 250 MB / 30,000 entry limit")
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("Unsafe path in repository archive")
            roots.add(path.parts[0])
            relative = PurePosixPath(*path.parts[1:])
            if member.isdir() or str(relative) == ".":
                continue
            if not member.isfile():
                omitted.append(str(relative))
                continue
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as output:
                output.write(source.read())
            target.chmod(0o700 if member.mode & 0o111 else 0o600)
        if len(roots) != 1:
            raise ValueError("Unexpected repository archive layout")
    return omitted


def expand_referenced_submodules(work, repo, pr, diff, files=()):
    return expand_context_submodules(work, repo, pr, diff, files,
                                     api=api, gh=gh, extract_snapshot=extract_snapshot, log=LOG)


def review_body(result, sha):
    if not isinstance(result.get("complete"), bool):
        raise ValueError("Missing review completeness decision")
    if not isinstance(result.get("findings"), list):
        raise ValueError("Missing review findings")
    for name in ("summary", "limitations"):
        if not isinstance(result.get(name), str) or not result[name].strip():
            raise ValueError(f"Missing {name}")
    validation = result.get("_validation")
    if validation and validation.get("status") in ("failed", "blocked", "timeout"):
        result["complete"] = False
        result["limitations"] += " " + validation_summary(validation)
    if not result["complete"] and not result["findings"]:
        return None
    sections, omissions = [], []
    policy = result.get("_review_policy")
    blocking = {item["index"] for item in policy["findings"] if item["blocking"]} if policy else None
    for index, finding in enumerate(result["findings"], 1):
        for name in ("severity", "file", "problem", "fix", "impact"):
            if not isinstance(finding.get(name), str) or not finding[name].strip():
                raise ValueError(f"Invalid finding {name}")
        if finding["severity"] not in ("Must fix", "Should fix", "Nice to have"):
            raise ValueError("Invalid severity")
        if type(finding.get("line")) is not int or finding["line"] < 1:
            raise ValueError("Invalid finding line")
        indent = " " * (len(str(index)) + 2)
        section = (f"{index}. **{finding['severity']}** — `{finding['file']}:{finding['line']}`\n\n"
                   f"{indent}- **Problem:** {finding['problem']}\n"
                   f"{indent}- [ ] **Fix:** {finding['fix']}\n"
                   f"{indent}- **If skipped:** {finding['impact']}")
        if policy and policy["round"] > 2:
            section += f"\n{indent}- **Review basis:** " + finding["origin_evidence"]
        (sections if blocking is None or index in blocking else omissions).append(section)
    event = "REQUEST_CHANGES" if sections else "COMMENT" if omissions else "APPROVE"
    body = ("**What this change does:** " + result["summary"] +
            "\n\n**Required changes:**\n\n" + "\n\n".join(sections)
            if sections else "LGTM. **What this change does:** " + result["summary"])
    if omissions:
        if not sections:
            body = "**Review follow-up:** " + result["summary"]
        body += ("\n\n**Reviewer omissions:** The findings below were missed in earlier rounds, or their "
                 "introduction by later edits could not be verified. They are comments only under "
                 "the two-round policy. This review does not grant approval.\n\n" + "\n\n".join(omissions))
    if not result["complete"]:
        body += "\n\n**Partial review:** Additional material or prior-finding resolution could not be verified."
    if policy:
        body += f"\n\n**Review round {policy['round']}:** Original findings are consolidated within the first two rounds."
        counts = policy["resolution_counts"]
        body += ("\n\n**Earlier findings:**\n\n"
                 "| Resolved | Unresolved | Unverified |\n"
                 "| ---: | ---: | ---: |\n"
                 f"| {counts['resolved']} | {counts['unresolved']} | {counts['unverified']} |")
    body += (f"\n\n**Validation:** {validation_summary(validation)}\n\n**Limits:** {result['limitations']}"
             f"\n\n**{CONFIG.get('display_name', 'PR Review Agent')}**. Reviewed commit `{sha}`.\n{MARKER}")
    if policy:
        body += "\n" + ledger_comment(policy, event)
    if len(body.encode()) > 60000:
        raise ValueError("Review body exceeds GitHub's size limit")
    return {"commit_id": sha, "event": event, "body": body}


def resolution_followup(result, sha):
    statuses = result.get("prior_finding_statuses", [])
    if not any(item["status"] == "resolved" for item in statuses):
        return None
    fingerprint = [sha, sorted([item["id"], item["status"]] for item in statuses)]
    digest = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
    marker = RESOLUTION_MARKER + digest + " -->"
    body = "**Review follow-up:** " + result["summary"] + "\n\n**Earlier findings:**\n\n"
    body += "\n\n".join(f"- **{item['status'].capitalize()}** — `{item['id']}`\n\n  {item['evidence']}"
                           for item in statuses)
    body += ("\n\n**Review deferred:** Overall validation or required evidence remains incomplete. "
             "Approval is withheld."
             f"\n\n**Validation:** {validation_summary(result.get('_validation'))}"
             f"\n\n**Limits:** {result['limitations']}"
             f"\n\n**{CONFIG.get('display_name', 'PR Review Agent')}**. Assessed commit `{sha}`.\n{marker}")
    if len(body.encode()) > 60000:
        raise ValueError("Resolution follow-up exceeds GitHub's size limit")
    return {"body": body, "marker": marker}


def publish_resolution_followup(repo, number, pr, result, watched, *, dry_run=False):
    update = resolution_followup(result, pr["head"]["sha"])
    if update is None:
        return True
    if any(update["marker"] in (comment.get("body") or "")
           and same_user(comment.get("user", {}).get("login", ""), CONFIG["user"])
           for comment in pr["conversation_comments"]):
        LOG.info("Resolution follow-up already posted for %s#%s at %s", repo, number, pr["head"]["sha"][:12])
        return True
    if dry_run:
        LOG.info("DRY RUN resolution follow-up %s#%s\n%s", repo, number, update["body"])
        return True
    fresh = api(f"repos/{repo}/pulls/{number}")
    if not same_revision(pr, fresh) or not eligible(fresh, CONFIG["user"], watched):
        LOG.info("Skip stale/closed/unassigned resolution follow-up for %s#%s", repo, number)
        return False
    for companion in result.get("_companion_prs", ()):
        if resolve_companion_pull(companion, api=api)["sha"] != companion["sha"]:
            LOG.info("Skip stale companion resolution follow-up for %s#%s", repo, number)
            return False
    if not repository_allowed(repo) or not same_user(api("user")["login"], CONFIG["user"]):
        raise RuntimeError("Resolution posting scope or GitHub account changed")
    posted = api(f"repos/{repo}/issues/{number}/comments", payload={"body": update["body"]})
    # Include our own comment in the saved revision so it cannot wake another model run.
    pr["conversation_comments"].append(posted)
    LOG.info("Posted resolution follow-up for %s#%s at %s: comment %s", repo, number,
             pr["head"]["sha"][:12], posted["id"])
    return True


def round_six_comment(repo, number, pr, reviews, history, outcome="REQUEST_CHANGES"):
    author = pr["user"]["login"]
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", author):
        raise ValueError("Invalid PR author login")
    evidence = {}
    for review in reviews:
        if review.get("commit_id") == pr["head"]["sha"]:
            continue
        recorded = review_history([review], CONFIG["user"], MARKER)
        if not recorded["request_changes_rounds"]:
            continue
        sha = review["commit_id"]
        if (not re.fullmatch(r"[a-f0-9]{40,64}", sha)
                or type(review.get("id")) is not int or review["id"] < 1):
            raise ValueError("Invalid review evidence")
        evidence[sha] = (review["id"], recorded["prior_findings"])
    if history["round"] < 6 or len(evidence) < 5:
        raise ValueError("Round-six notice requires five distinct change-request commits")
    assessment = {
        "REQUEST_CHANGES": "The latest commit was reviewed and received a new **REQUEST_CHANGES** review.",
        "COMMENT": "The latest commit was reviewed. Findings were posted in a separate **COMMENT** review under the existing review policy; approval was not granted.",
        "DEFERRED": "The latest commit was assessed, but missing evidence or incomplete validation prevented a reliable decision. The review is **deferred**; no new code defect is asserted by this notice.",
    }[outcome]
    body = (
        f"@{author}\n\n**Status:** Draft.\n\n"
        f"**Current assessment:** {assessment}\n\n"
        f"**Reason:** This PR has reached review round **{history['round']}** after "
        f"**{len(evidence)} distinct commits received change requests**. "
        "Merge readiness has not been confirmed after these issue-fixing rounds, so the PR is being kept in **draft**.\n\n"
        "**Before marking it ready:**\n\n"
        "- [ ] **Issues:** Resolve the documented issues and verify the fixes against the latest commits.\n"
        "- [ ] **Validation:** Run the relevant builds and tests.\n"
        "- [ ] **Merge readiness:** Make sure the PR is ready to merge before marking it **Ready for review** again.\n\n"
        "**Evidence from earlier reviewed commits:**\n\n"
        "| Commit | Change-request review | Recorded issue examples |\n"
        "| --- | --- | --- |\n"
    )
    for sha, (review_id, findings) in list(evidence.items())[-5:]:
        issues = []
        for finding in findings[:2]:
            problem = re.search(r"(?m)^\s*- (?:\*\*)?Problem:(?:\*\*)?\s*(.+)$", finding["text"])
            summary = " ".join(problem[1].split()) if problem else "Recorded blocking finding."
            if len(summary) > 300:
                summary = summary[:297].rsplit(" ", 1)[0] + "..."
            summary = html.escape(summary, quote=False).replace("@", "@\u200b").replace("|", "&#124;")
            path = html.escape(finding["file"], quote=False).replace("`", "").replace("@", "@\u200b")
            if len(path) > 200:
                path = path[:197] + "..."
            path = path.replace("|", r"\|")
            issues.append(f"`{path}:{finding['line']}` — {summary}")
        body += (f"| [`{sha[:12]}`](https://github.com/{repo}/commit/{sha}) "
                 f"| [Review {review_id}](https://github.com/{repo}/pull/{number}"
                 f"#pullrequestreview-{review_id}) | " + "<br><br>".join(issues) + " |\n")
    body += (
        "\n**Evidence scope:** These links record the issues reported on those revisions. Verify their resolution "
        "against the current head before requesting another review.\n\n"
        f"**Current head:** [`{pr['head']['sha'][:12]}`]"
        f"(https://github.com/{repo}/commit/{pr['head']['sha']}).\n\n"
        f"**{CONFIG.get('display_name', 'PR Review Agent')}** — review-readiness notice.\n" + ROUND_SIX_MARKER
    )
    if len(body.encode()) > 60000:
        raise ValueError("Round-six notice exceeds GitHub's size limit")
    return body


def apply_round_six(repo, number, pr, reviews, state, history, outcome="REQUEST_CHANGES"):
    body = round_six_comment(repo, number, pr, reviews, history, outcome)
    endpoint = f"repos/{repo}/pulls/{number}"
    key = f"{repo}#{number}"
    fresh = api(endpoint)
    watched = state.get(key, {}).get("watch", False)
    if not same_revision(pr, fresh) or not eligible(fresh, CONFIG["user"], watched, include_draft=True):
        LOG.info("Skip stale/closed/unassigned round-six action for %s", key)
        return False
    if not repository_allowed(repo) or not same_user(api("user")["login"], CONFIG["user"]):
        raise RuntimeError("Round-six posting scope or GitHub account changed")
    comments = paginated(f"repos/{repo}/issues/{number}/comments?per_page=100")
    existing = next((comment for comment in comments
                     if ROUND_SIX_MARKER in (comment.get("body") or "")
                     and same_user(comment.get("user", {}).get("login", ""), CONFIG["user"])), None)
    if existing is None:
        entry = state.setdefault(key, {})
        entry["watch"] = True
        entry.setdefault("round_six", {}).update(pending=True, round=history["round"])
        save_state(state)
        if not fresh["draft"]:
            mutation = """mutation($id: ID!) {
                convertPullRequestToDraft(input: {pullRequestId: $id}) {
                    pullRequest { isDraft }
                }
            }"""
            result = api("graphql", payload={"query": mutation, "variables": {"id": fresh["node_id"]}})
            converted = ((result.get("data") or {}).get("convertPullRequestToDraft") or {}).get("pullRequest") or {}
            if result.get("errors") or converted.get("isDraft") is not True:
                raise RuntimeError("GitHub did not confirm conversion to draft")
        after = api(endpoint)
        if after["state"] != "open" or not same_revision(pr, after):
            LOG.info("Round-six PR changed before its notice; retry later: %s", key)
            return False
        if not after["draft"]:
            raise RuntimeError("PR is no longer draft; refusing to post a misleading draft notice")
        if not repository_allowed(repo) or not same_user(api("user")["login"], CONFIG["user"]):
            raise RuntimeError("Round-six comment scope or GitHub account changed")
        existing = api(f"repos/{repo}/issues/{number}/comments", payload={"body": body})
    # A remote marker recovers a lost POST response and respects an author's later Ready action.
    entry = state.setdefault(key, {})
    entry["watch"] = True
    entry.setdefault("round_six", {}).update(
        completed=True, pending=False, round=history["round"], comment_id=existing["id"])
    save_state(state)
    LOG.info("Round-six draft notice completed for %s: comment %s", key, existing["id"])
    return True


def finish_round_six(repo, number, pr, reviews, state, notifier):
    record = state[f"{repo}#{number}"]["round_six"]
    if record["outcome"] == "APPROVE":
        record.update(pending=False, completed=True)
    elif not record.get("completed"):
        if not apply_round_six(repo, number, pr, reviews, state,
                               {"round": record["round"]}, record["outcome"]):
            return
    record["pending"] = False
    save_state(state)
    if notifier:
        notifier.observe(repo, number, deferred=record["outcome"] == "DEFERRED",
                         round_number=record["round"])


def prepare_context(work, repo, number, pr, reviews):
    omissions = {}
    for side in ("head", "base"):
        destination = work / side
        destination.mkdir()
        data = gh("api", "--hostname", "github.com", f"repos/{repo}/tarball/{pr[side]['sha']}")
        omissions[side] = extract_snapshot(data, destination)
    diff = gh("api", "--hostname", "github.com", "-H", "Accept: application/vnd.github.diff",
              f"repos/{repo}/pulls/{number}")
    if len(diff) > 5_000_000:
        raise ValueError("Diff exceeds the 5 MB review limit")
    (work / "pr.diff").write_bytes(diff)
    files = paginated(f"repos/{repo}/pulls/{number}/files?per_page=100")
    if len(files) != pr["changed_files"]:
        raise ValueError("Incomplete changed-file list; review deferred")
    submodules = expand_referenced_submodules(work, repo, pr, diff.decode(errors="replace"), files)
    conversation_comments = (pr["conversation_comments"] if "conversation_comments" in pr else
                             paginated(f"repos/{repo}/issues/{number}/comments?per_page=100"))
    review_comments = (pr["review_comments"] if "review_comments" in pr else
                       paginated(f"repos/{repo}/pulls/{number}/comments?per_page=100"))
    companions = collect_companion_context(
        work, files, pr, sources=submodule_sources(work / "base"), api=api, gh=gh,
        extract_snapshot=extract_snapshot, log=LOG,
        context_repositories=[*CONFIG.get("repositories", ()),
                              *CONFIG.get("context_repositories", ())],
        comments=[*conversation_comments, *review_comments])
    history = review_history(reviews, CONFIG["user"], MARKER)
    prepare_history(work, repo, history, submodules, api=api, gh=gh, extract_snapshot=extract_snapshot)
    LOG.info("Review policy %s#%s: round %s, %s earlier findings", repo, number,
             history["round"], len(history["prior_findings"]))
    context = {"repository": repo, "number": number, "title": pr["title"],
               "description": pr.get("body"), "head": pr["head"]["sha"],
               "base": pr["base"]["sha"], "files": files, "previous_reviews": reviews,
               "conversation_comments": conversation_comments, "review_comments": review_comments,
               "omitted_archive_links": omissions, "expanded_submodules": submodules,
               "companion_evidence": companions, "context_policy": deferred_revision(pr)[2],
               "review_history": history}
    (work / "context.json").write_text(json.dumps(context, indent=2))
    fresh = api(f"repos/{repo}/pulls/{number}")
    if not same_revision(pr, fresh):
        raise ValueError("PR changed while downloading; retry next poll")
    return context


def evaluate(repo, number, pr, reviews):
    with tempfile.TemporaryDirectory(prefix="review-", dir=ROOT / "work") as work_dir:
        work = Path(work_dir)
        context = prepare_context(work, repo, number, pr, reviews)
        validation = run_checks(work, repo, context, CONFIG.get("validation", {}),
                                api=api, gh=gh, extract_snapshot=extract_snapshot, log=LOG)
        context["validation"] = validation
        (work / "context.json").write_text(json.dumps(context, indent=2))
        (work / "validation.json").write_text(json.dumps(validation, indent=2))
        (ROOT / "last-validation.json").write_text(json.dumps({"repository": repo, "number": number,
                                                               "head": pr["head"]["sha"],
                                                               "validation": validation}, indent=2) + "\n")
        skill_text = "\n\n".join(Path(p).read_text() for p in CONFIG["skill_files"])
        prompt = asset(ROOT, "review-prompt.md").read_text() + "\n\nTRUSTED REVIEW RULES:\n" + skill_text
        required_ids = [item["id"] for item in context["review_history"]["prior_findings"]]
        prompt += ("\n\nCONTROLLER VERIFICATION CONTRACT:\n"
                   f"Return exactly {len(required_ids)} prior_finding_statuses items, one per ID below. "
                   "Do not group or omit IDs, even when findings overlap or complete=false. "
                   "If a finding cannot be verified, use unverified with the actual reason and complete=false. "
                   "Never invent resolution evidence. Required IDs:\n" + json.dumps(required_ids))
        schema = json.loads(asset(ROOT, "review-schema.json").read_text())
        statuses = schema["properties"]["prior_finding_statuses"]
        statuses.update(minItems=len(required_ids), maxItems=len(required_ids))
        if required_ids:
            statuses["items"]["properties"]["id"]["enum"] = required_ids
        schema_path = work / "result-schema.json"
        schema_path.write_text(json.dumps(schema))
        output = work / "result.json"
        # The reviewer has no network, plugins, repo hooks, or write permission. Only this runner posts.
        environment = {k: v for k, v in os.environ.items()
                       if not (k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                                     "GITHUB_ENTERPRISE_TOKEN", "OPENAI_API_KEY")
                               or k.startswith("CODEX_THREAD") or k == "CODEX_SANDBOX")}
        model, reasoning = review_model()
        LOG.info("Review model %s#%s: %s, reasoning=%s", repo, number, model, reasoning)
        if "ai" in CONFIG:
            from ai_runtime import run_agent
            result = run_agent(CONFIG, prompt, schema, work, model=model, reasoning=reasoning)
            output.write_text(json.dumps(result))
        else:
            command([CONFIG["codex"], "--no-daemon", "-a", "never", "exec",
                     "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only", "--ephemeral",
                     "--skip-git-repo-check", "--cd", str(work), "--model", model,
                     "-c", 'model_reasoning_effort="' + reasoning + '"',
                     "--output-schema", str(schema_path),
                     "--output-last-message", str(output), "-"],
                    input_text=prompt, cwd=work, timeout=1800, env=environment)
            result = json.loads(output.read_text())
        apply_policy(result, context["review_history"], work, pr["head"]["sha"])
        result["_validation"] = validation
        result["_companion_prs"] = context["companion_evidence"]["pull_requests"]
        result["_companion_unavailable"] = any(
            item.get("repository") for item in context["companion_evidence"]["unavailable"])
        # Retain the latest result for diagnosis, without accumulating repository snapshots.
        (ROOT / "last-review.json").write_text(json.dumps({"repository": repo, "number": number,
                                                         "head": pr["head"]["sha"],
                                                         "base": pr["base"]["sha"],
                                                         "model": model, "reasoning": reasoning,
                                                         "review": result}, indent=2) + "\n")
        return result


def review_one(repo, number, state, *, dry_run=False, notifier=None):
    if dry_run:
        notifier = None
    if not repository_allowed(repo):
        LOG.info("Skip repository outside configured scope: %s", repo)
        return
    endpoint = f"repos/{repo}/pulls/{number}"
    key = f"{repo}#{number}"
    pr = api(endpoint)
    if pr["state"] != "open":
        if not dry_run:
            state.pop(key, None)
            save_state(state)
            if notifier:
                notifier.observe(repo, number, deferred=False)
        return
    watched = state.get(key, {}).get("watch", False)
    round_policy = CONFIG.get("draft_at_round_six", False)
    round_six = state.get(key, {}).get("round_six", {})
    if not eligible(pr, CONFIG["user"], watched, include_draft=True):
        if not dry_run:
            state.pop(key, None)
            save_state(state)
            if notifier:
                notifier.observe(repo, number, deferred=False)
        return
    revision_pair = [pr[side]["sha"] for side in ("head", "base")]
    pending_round_six = bool(round_policy and round_six.get("pending")
                             and round_six.get("reviewed_revision") == revision_pair)
    if pr["draft"] and (dry_run or not pending_round_six):
        LOG.info("Waiting for author to mark draft PR ready for review: %s", key)
        if notifier:
            notifier.observe(repo, number, deferred=False)
        return
    for side in ("head", "base"):
        if not re.fullmatch(r"[a-f0-9]{40,64}", pr[side]["sha"]):
            raise ValueError("Invalid commit ID")
    reviews = paginated(endpoint + "/reviews?per_page=100")
    history = review_history(reviews, CONFIG["user"], MARKER) if round_policy else None
    if notifier:
        notifier.observe_reviews(repo, number, reviews, CONFIG["user"], MARKER, pr["head"]["sha"])
    if pending_round_six and not dry_run:
        expected = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}
        confirmed = any(review.get("state") == expected.get(round_six.get("outcome"))
                        and review.get("commit_id") == pr["head"]["sha"]
                        and same_user(review.get("user", {}).get("login", ""), CONFIG["user"])
                        and MARKER in (review.get("body") or "") for review in reviews)
        if confirmed or round_six.get("outcome") == "DEFERRED":
            # Finish an interrupted notice/SMS; this never evaluates or posts another review.
            finish_round_six(repo, number, pr, reviews, state, notifier)
            return
    if pr["draft"]:
        LOG.info("Waiting for author to mark draft PR ready for review: %s", key)
        if notifier:
            notifier.observe(repo, number, deferred=False)
        return
    if already_reviewed(reviews, CONFIG["user"], pr["head"]["sha"]):
        LOG.info("Already reviewed %s at %s", key, pr["head"]["sha"][:12])
        if notifier:
            notifier.observe(repo, number, deferred=False)
        return
    pr = {**pr,
          "conversation_comments": paginated(f"repos/{repo}/issues/{number}/comments?per_page=100"),
          "review_comments": paginated(endpoint + "/comments?per_page=100")}
    revision = deferred_revision(pr)
    saved_revision = state.get(key, {}).get("deferred_revision")
    if saved_revision and len(saved_revision) in (4, 5) and saved_revision[:3] == revision[:3]:
        companions = [resolve_companion_pull({"repository": repository, "number": number}, api=api)
                      for repository, number, _ in saved_revision[3]]
        dependencies = []
        if len(saved_revision) == 5:
            rules = CONFIG.get("validation", {}).get("workspace_dependencies", {}).values()
            trusted = {(rule.get("repository"), rule.get("branch")) for rule in rules if rule.get("branch")}
            for repository, branch, _ in saved_revision[4]:
                if (repository, branch) not in trusted:
                    raise ValueError("Deferred dependency branch is not configured")
                dependencies.append({"repository": repository, "branch": branch,
                                     "sha": resolve_branch_revision(repository, branch, api=api)})
        revision = deferred_revision(pr, companions, dependencies)
    retry_at = state.get(key, {}).get("deferred_retry_at")
    if (saved_revision == revision
            and (retry_at is None or retry_at > time.time())):
        LOG.info("Deferred revision and companion context unchanged for %s at %s; waiting for new context or commits",
                 key, pr["head"]["sha"][:12])
        if notifier:
            notifier.observe(repo, number, deferred=True,
                             round_number=review_history(reviews, CONFIG["user"], MARKER)["round"])
        return
    if state.get(key, {}).get("deferred_revision"):
        LOG.info("Retrying deferred %s after revision, comments, companion context, policy change, or retry deadline", key)
    # Persist before POST so an ambiguous network response cannot lose a watched PR.
    if not dry_run:
        state[key] = {"last_attempt": time.time(), "watch": watched}
        if round_six:
            state[key]["round_six"] = round_six
        save_state(state)
    LOG.info("Reviewing %s at %s%s", key, pr["head"]["sha"][:12], " (dry run)" if dry_run else "")
    if notifier and not round_policy:
        notifier.observe(repo, number, round_number=review_history(reviews, CONFIG["user"], MARKER)["round"])
    result = evaluate(repo, number, pr, reviews)
    payload = review_body(result, pr["head"]["sha"])
    at_round_six = bool(round_policy and history["round"] >= 6)
    if payload is None:
        if not publish_resolution_followup(repo, number, pr, result, watched, dry_run=dry_run):
            return
        LOG.warning("Deferred %s: %s", key, result["limitations"])
        if not dry_run:
            state[key]["deferred_revision"] = deferred_revision(
                pr, result.get("_companion_prs", ()),
                result.get("_validation", {}).get("dependencies", ()))
            if (result.get("_validation", {}).get("status") in ("blocked", "timeout")
                    or result.get("_companion_unavailable")):
                delay = min(max(int(CONFIG.get("validation", {}).get("retry_seconds", 3600)), 600), 86400)
                state[key]["deferred_retry_at"] = time.time() + delay
            save_state(state)
            if at_round_six:
                record = state[key].setdefault("round_six", {})
                record.update(reviewed_revision=revision_pair, outcome="DEFERRED",
                              round=history["round"], pending=True)
                save_state(state)
                finish_round_six(repo, number, pr, reviews, state, notifier)
                return
            if notifier:
                notifier.observe(repo, number, deferred=True)
        return
    if notifier:
        notifier.observe(repo, number, deferred=False)
    fresh = api(endpoint)
    if (not same_revision(pr, fresh)
            or not eligible(fresh, CONFIG["user"], watched)):
        LOG.info("Skip stale/closed/unassigned review for %s", key)
        return
    for companion in result.get("_companion_prs", ()):
        if resolve_companion_pull(companion, api=api)["sha"] != companion["sha"]:
            LOG.info("Skip stale companion context for %s: %s#%s changed during review",
                     key, companion["repository"], companion["number"])
            return
    if already_reviewed(paginated(endpoint + "/reviews?per_page=100"),
                        CONFIG["user"], pr["head"]["sha"]):
        LOG.info("Review submitted elsewhere while evaluating %s; skipping", key)
        return
    if dry_run:
        LOG.info("DRY RUN %s %s\n%s", payload["event"], key, payload["body"])
        if at_round_six and payload["event"] != "APPROVE" and not round_six.get("completed"):
            LOG.info("DRY RUN then keep draft and post separate author notice for %s", key)
        return
    if not repository_allowed(repo):
        LOG.info("Repository removed from scope while reviewing: %s", repo)
        return
    if not same_user(api("user")["login"], CONFIG["user"]):
        raise RuntimeError("GitHub account changed during review; refusing to post")
    state[key]["watch"] = True
    if at_round_six:
        record = state[key].setdefault("round_six", {})
        record.update(reviewed_revision=revision_pair, outcome=payload["event"],
                      round=history["round"], pending=True)
    save_state(state)
    posted = api(endpoint + "/reviews", payload=payload)
    LOG.info("Posted %s %s: %s", payload["event"], key, posted["html_url"])
    if at_round_six:
        finish_round_six(repo, number, pr, [*reviews, posted], state, notifier)
    if notifier and payload["event"] == "APPROVE":
        notifier.observe(repo, number, deferred=False, approval_sha=pr["head"]["sha"])


def main():
    global CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check login and discover PRs; no model or posting")
    parser.add_argument("--dry-run", action="store_true", help="Review but do not post or start tracking")
    parser.add_argument("--limit", type=int, help="Maximum PRs considered in this run")
    parser.add_argument("--pr", help="Only this discovered/watched owner/repo#number")
    args = parser.parse_args()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG = load_config(ROOT)
    if not CONFIG.get("repositories"):
        raise ValueError("Configure an explicit repository allowlist before running")
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(ROOT / "reviewer.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.addHandler(logging.StreamHandler())
    (ROOT / "work").mkdir(exist_ok=True)
    with (ROOT / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another poll is still running")
            return
        if not (args.check or args.dry_run) and not claim_poll_window():
            return
        user = api("user")["login"]
        if not same_user(user, CONFIG["user"]):
            raise RuntimeError(f"GitHub account changed from {CONFIG['user']} to {user}; stopped")
        search = ["search", "prs", "--review-requested", user, "--state", "open",
                  "--limit", "1000", "--json", "number,repository,url"]
        for repo in CONFIG["repositories"]:
            search += ["--repo", repo]
        discovered = [pr for pr in json.loads(gh(*search))
                      if repository_allowed(pr["repository"]["nameWithOwner"])]
        state_path = ROOT / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        allowed_state = {key: value for key, value in state.items()
                         if repository_allowed(key.rsplit("#", 1)[0])}
        if len(allowed_state) != len(state) and not args.check and not args.dry_run:
            save_state(allowed_state)
        state = allowed_state
        candidates = {f"{p['repository']['nameWithOwner']}#{p['number']}" for p in discovered} | set(state)
        LOG.info("Poll: %s; %d requested, %d watched", user, len(discovered), len(state))
        if len(discovered) == 1000:
            LOG.warning("GitHub search reached its 1000-result cap")
        if args.check:
            for key in sorted(candidates):
                LOG.info("Candidate %s", key)
            return
        if args.pr:
            if args.pr not in candidates:
                raise ValueError("PR is not requested or watched")
            candidates = {args.pr}
        ordered = sorted(candidates, key=lambda key: (state.get(key, {}).get("last_attempt", 0), key))
        notifier = (SmsNotifier(ROOT, CONFIG["sms_notifications"], LOG)
                    if not args.dry_run and CONFIG.get("sms_notifications", {}).get("enabled") else None)
        failed = False
        for key in ordered[:args.limit]:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", key):
                LOG.error("Invalid PR reference: %r", key)
                continue
            repo, number = key.rsplit("#", 1)
            try:
                review_one(repo, int(number), state, dry_run=args.dry_run, notifier=notifier)
            except Exception:
                failed = True
                LOG.exception("Review failed for %s; retry on a later poll", key)
        LOG.info("Poll complete")
        if failed:
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Poll failed; retry on the next allowed poll window")
        raise SystemExit(1)
