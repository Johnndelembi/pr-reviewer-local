"""Consolidate original findings into two rounds and verify later finding origins."""
import json
from pathlib import PurePosixPath
import re
import subprocess

from submodule_context import submodule_sources


LEDGER = "codex-review-ledger:v1"
SECTION = re.compile(r"(?m)^(\d+)\. \*\*(Must fix|Should fix|Nice to have)\*\* — `([^`\n]+):(\d+)`")


def review_history(reviews, user, marker):
    reviewed_heads, findings, previous = set(), {}, None
    for review in reviews:
        body = review.get("body") or ""
        if (review["user"]["login"].casefold() != user.casefold() or marker not in body
                or review["state"] not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED")):
            continue
        receipt = re.search(r"<!-- " + LEDGER + r" (.*?) -->", body)
        metadata = json.loads(receipt[1]) if receipt else None
        sections = list(SECTION.finditer(body))
        requested = (metadata["event"] == "REQUEST_CHANGES" if metadata else
                     review["state"] == "CHANGES_REQUESTED" or
                     (review["state"] == "DISMISSED" and bool(sections)))
        if requested:
            if not sections:
                raise ValueError("Previous automated change request has no readable finding ledger")
            reviewed_heads.add(review["commit_id"])
            records = {item["index"]: item for item in metadata["findings"]} if metadata else {}
            for index, section in enumerate(sections):
                ordinal = int(section[1])
                record = records.get(ordinal)
                if metadata and record is None:
                    raise ValueError("Previous automated finding is missing its ledger record")
                if record and not record["blocking"]:
                    continue
                identifier = record["id"] if record else f"review-{review['id']}-finding-{ordinal}"
                end = sections[index + 1].start() if index + 1 < len(sections) else len(body)
                text = re.split(r"\n\n(?:\*\*)?Validation:(?:\*\*)?", body[section.start():end], maxsplit=1)[0]
                findings[identifier] = {"id": identifier, "review_id": review["id"],
                                        "reviewed_head": review["commit_id"], "file": section[3],
                                        "line": int(section[4]), "severity": section[2], "text": text}
        previous = review["commit_id"]
    if previous and not re.fullmatch(r"[a-f0-9]{40}", previous):
        raise ValueError("Invalid previous reviewed commit")
    return {"request_changes_rounds": len(reviewed_heads), "round": len(reviewed_heads) + 1, "discovery_rounds": 2,
            "previous_head": previous, "prior_findings": list(findings.values()),
            "previous_paths": {}}


def prepare_history(work, repo, history, expanded, *, api, gh, extract_snapshot):
    previous = history["previous_head"]
    if previous is None:
        return
    root = work / "previous-review"
    root.mkdir()
    omissions = extract_snapshot(gh("api", "--hostname", "github.com",
                                     f"repos/{repo}/tarball/{previous}"), root)
    if omissions:
        history["omitted_archive_links"] = omissions
    sources = submodule_sources(root)
    current = {path.removeprefix("head/"): item["repository"]
               for path, item in expanded.items() if path.startswith("head/")}
    needed = {name.casefold() for name in current.values()}
    selected = {path: name for path, name in sources.items() if name.casefold() in needed}
    if len(selected) > 8:
        raise ValueError("More than eight previous-review submodules; split this review")
    if selected:
        tree = api(f"repos/{repo}/git/trees/{previous}?recursive=1")
        if tree.get("truncated"):
            raise ValueError("Previous-review submodule tree is incomplete")
        links = {item["path"]: item["sha"] for item in tree["tree"] if item["mode"] == "160000"}
        for path, name in selected.items():
            sha = links.get(path)
            if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise ValueError("Previous-review submodule gitlink is unavailable")
            target = root / path
            target.mkdir(parents=True, exist_ok=True)
            omitted = extract_snapshot(gh("api", "--hostname", "github.com",
                                          f"repos/{name}/tarball/{sha}"), target)
            if omitted:
                history.setdefault("omitted_archive_links", []).extend(f"{path}/{p}" for p in omitted)
        for path, name in current.items():
            matches = [old for old, source in selected.items() if source.casefold() == name.casefold()]
            if path in matches:
                history["previous_paths"][path] = path
            elif len(matches) == 1:
                history["previous_paths"][path] = matches[0]
            elif len(matches) > 1:
                raise ValueError("Ambiguous previous-review submodule path")
    delta = work / "since-previous-review.diff"
    with delta.open("wb") as output:
        result = subprocess.run(["git", "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--find-renames",
                                 "previous-review", "head"], cwd=work, stdout=output,
                                stderr=subprocess.PIPE, timeout=120)
    if result.returncode not in (0, 1):
        raise ValueError("Could not compare the previous reviewed source")
    if delta.stat().st_size > 5_000_000:
        raise ValueError("Review follow-up diff exceeds the 5 MB limit")
    renamed = subprocess.run(["git", "diff", "--no-index", "--no-ext-diff", "--no-textconv",
                              "--find-renames", "--name-status", "-z", "previous-review", "head"],
                             cwd=work, capture_output=True, timeout=120)
    if renamed.returncode not in (0, 1) or len(renamed.stdout) > 5_000_000:
        raise ValueError("Could not map renamed files in the review follow-up")
    records = iter(renamed.stdout.decode(errors="surrogateescape").rstrip("\0").split("\0"))
    for status in records:
        if not status:
            continue
        before = next(records)
        if status.startswith(("R", "C")):
            after = next(records)
            if status.startswith("R") and before.startswith("previous-review/") and after.startswith("head/"):
                history["previous_paths"][after.removeprefix("head/")] = before.removeprefix("previous-review/")
    history["previous_snapshot"] = "previous-review/"
    history["incremental_diff"] = delta.name


def _changed_line(work, history, finding):
    path, line, side = (finding.get("change_file"), finding.get("change_line"),
                        finding.get("change_side"))
    if (not isinstance(path, str) or not path or "\\" in path
            or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
            or type(line) is not int or line < 1 or side not in ("head", "previous")
            or not history.get("previous_snapshot")):
        return False
    mappings = sorted(history["previous_paths"].items(), key=lambda item: -len(item[0]))
    if side == "previous":
        for current, previous in sorted(mappings, key=lambda item: -len(item[1])):
            if path == previous or path.startswith(previous + "/"):
                path = current + path[len(previous):]
                break
    previous_path = path
    for current, previous in mappings:
        if path == current or path.startswith(current + "/"):
            previous_path = previous + path[len(current):]
            break
    def source_file(root, relative):
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Finding evidence escapes its source snapshot")
        return str(target) if target.is_file() else "/dev/null"
    before = source_file(work / "previous-review", previous_path)
    after = source_file(work / "head", path)
    result = subprocess.run(["git", "diff", "--no-index", "--no-ext-diff", "--no-textconv",
                             "--unified=0", "--", before, after], cwd=work,
                            capture_output=True, timeout=30)
    if result.returncode not in (0, 1) or len(result.stdout) > 5_000_000:
        raise ValueError("Could not verify the finding's incremental change")
    for match in re.finditer(rb"(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", result.stdout):
        start = int(match[3] if side == "head" else match[1])
        length = match[4] if side == "head" else match[2]
        if start <= line < start + (int(length) if length is not None else 1):
            return True
    return False


def apply_policy(result, history, work, sha):
    prior = {item["id"]: item for item in history["prior_findings"]}
    statuses = result.get("prior_finding_statuses")
    if not isinstance(statuses, list):
        raise ValueError("Missing prior-finding verification")
    by_id = {}
    for item in statuses:
        if (item.get("id") not in prior or item["id"] in by_id
                or item.get("status") not in ("resolved", "unresolved", "unverified")
                or not isinstance(item.get("evidence"), str) or not item["evidence"].strip()):
            raise ValueError("Invalid prior-finding verification")
        by_id[item["id"]] = item["status"]
    if set(by_id) != set(prior):
        raise ValueError("Every earlier finding must be verified before completing a follow-up")
    if "unverified" in by_id.values():
        result["complete"] = False
    records, unresolved = [], set()
    for index, finding in enumerate(result["findings"], 1):
        origin, identifier = finding.get("origin"), finding.get("prior_finding_id")
        if origin not in ("original", "unresolved", "regression", "late_discovery"):
            raise ValueError("Missing finding origin")
        if not isinstance(finding.get("origin_evidence"), str) or not finding["origin_evidence"].strip():
            raise ValueError("Missing finding-origin evidence")
        if origin == "unresolved":
            if identifier not in prior or by_id[identifier] != "unresolved" or identifier in unresolved:
                raise ValueError("Unresolved finding must match an earlier blocking finding")
            unresolved.add(identifier)
        else:
            if identifier is not None:
                raise ValueError("A fresh finding cannot claim an earlier finding ID")
            identifier = f"{sha}-finding-{index}"
        blocking = history["round"] <= 2 or origin == "unresolved"
        if history["round"] > 2 and origin == "regression":
            blocking = _changed_line(work, history, finding)
        if history["round"] > 2 and not blocking:
            finding["origin"] = "late_discovery"
        records.append({"id": identifier, "index": index, "blocking": blocking})
    if unresolved != {identifier for identifier, status in by_id.items() if status == "unresolved"}:
        raise ValueError("All unresolved earlier findings must appear in the consolidated review")
    result["_review_policy"] = {"round": history["round"], "findings": records,
                                "resolution_counts": {status: sum(value == status for value in by_id.values())
                                                      for status in ("resolved", "unresolved", "unverified")}}
    return result


def ledger_comment(policy, event):
    return "<!-- " + LEDGER + " " + json.dumps({**policy, "event": event}, separators=(",", ":")) + " -->"
