"""Prepare disposable build inputs and collect controller-owned check results."""
import base64
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import quote

from check_sandbox import run_sandbox
from submodule_context import submodule_sources
from validation_plan import discover_targets, plan_checks


def _public_copy(root):
    # Only disposable downloaded source/manifest copies are exposed to container UID1000.
    root.chmod(0o755)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Validation inputs must not contain host symlinks")
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)


def resolve_branch_revision(repository, branch, *, api):
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid configured validation dependency repository")
    if (not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", branch)
            or any(part in (".", "..") for part in branch.split("/"))):
        raise ValueError("Invalid validation dependency branch")
    sha = api(f"repos/{repository}/commits/{quote(branch, safe='')}").get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid validation dependency pin")
    return sha


def add_workspace_dependencies(head, settings, *, api, gh, extract_snapshot, log):
    """Fill missing workspace packages from a configured branch/revision or approved parent pin."""
    manifests = [path for path in head.rglob("package.json")
                 if not any(part in ("node_modules", "fixtures", "__fixtures__", "test", "tests")
                            for part in path.relative_to(head).parts[:-1])]
    if len(manifests) > 128:
        raise ValueError("More than 128 build package manifests")
    present, needed = set(), set()
    for manifest in manifests:
        if manifest.stat().st_size > 1_000_000:
            continue
        data = json.loads(manifest.read_text())
        present.add(data.get("name"))
        for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name, spec in data.get(field, {}).items():
                if isinstance(spec, str) and spec.startswith("workspace:"):
                    needed.add(name)
    records = []
    for name in sorted(needed - present):
        rule = settings.get("workspace_dependencies", {}).get(name)
        if not rule:
            continue  # The planner reports missing dependencies as blocked.
        parent, repository = rule["parent_repository"], rule["repository"]
        for value in (parent, repository):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
                raise ValueError("Invalid configured validation dependency repository")
        parent_sha = None
        if "revision" in rule and "branch" in rule:
            raise ValueError("Configure either revision or branch for a validation dependency")
        if "revision" in rule:
            sha = rule["revision"]
            if not isinstance(sha, str):
                raise ValueError("Invalid validation dependency pin")
            origin = "configured revision"
        elif "branch" in rule:
            sha = resolve_branch_revision(repository, rule["branch"], api=api)
            origin = f"configured branch {rule['branch']}"
        else:
            branch = api(f"repos/{parent}")["default_branch"]
            parent_sha = api(f"repos/{parent}/commits/{branch}")["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", parent_sha):
                raise ValueError("Invalid approved parent revision")
            metadata = api(f"repos/{parent}/contents/.gitmodules?ref={parent_sha}")
            approved = head.parent / "dependency-approval"
            approved.mkdir(exist_ok=True)
            (approved / ".gitmodules").write_bytes(base64.b64decode(metadata["content"]))
            paths = [path for path, source in submodule_sources(approved).items()
                     if source.casefold() == repository.casefold()]
            tree = api(f"repos/{parent}/git/trees/{parent_sha}?recursive=1")
            if tree.get("truncated"):
                raise ValueError("Approved dependency tree is incomplete")
            pins = {entry["sha"] for entry in tree["tree"]
                    if entry["path"] in paths and entry["mode"] == "160000"}
            if len(pins) != 1:
                raise ValueError(f"No unique approved parent pin for {name}")
            sha = pins.pop()
            origin = "approved parent"
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("Invalid validation dependency pin")
        target = head / ".reviewer-dependencies" / repository.rsplit("/", 1)[-1]
        target.mkdir(parents=True, exist_ok=False)
        data = gh("api", "--hostname", "github.com", f"repos/{repository}/tarball/{sha}")
        extract_snapshot(data, target)
        if json.loads((target / "package.json").read_text()).get("name") != name:
            raise ValueError("Approved dependency package name does not match")
        records.append({"package": name, "repository": repository, "sha": sha,
                        "parent_repository": parent, "parent_sha": parent_sha,
                        "path": str(target.relative_to(head)),
                        "note": f"Build-only dependency from {origin}; not a pin declared by this standalone PR"})
        if "branch" in rule:
            records[-1]["branch"] = rule["branch"]
        log.info("Validation dependency %s from %s@%s (%s%s)", name, repository, sha,
                 origin, f" {parent_sha}" if parent_sha else "")
    return records


def run_checks(work, repo, context, settings, *, api, gh, extract_snapshot, log):
    report = {"enabled": bool(settings.get("enabled")), "status": "skipped", "jobs": [],
              "dependencies": [], "reason": "Build/test execution is disabled"}
    if not report["enabled"]:
        return report
    # Neither dependency installation nor build output may modify review snapshots.
    try:
        if not discover_targets(work, repo, context):
            report.update(status="not_applicable", reason="No changed executable package was selected")
            return report
        scratch = work / "validation-work"
        scratch.mkdir()
        head = scratch / "head"
        shutil.copytree(work / "head", head)
        report["dependencies"] = add_workspace_dependencies(
            head, settings, api=api, gh=gh, extract_snapshot=extract_snapshot, log=log)
        plans = plan_checks(scratch, repo, context, settings)
        _public_copy(head)
        started = time.monotonic()
        budget = min(max(int(settings.get("total_timeout_seconds", 1800)), 60), 3600)
        for index, plan in enumerate(plans):
            details = {key: value for key, value in plan.items()
                       if key not in ("source", "dependency_input", "stages")}
            log.info("Validation target %s (%s)", plan["name"], plan.get("kind", "unsupported"))
            if plan.get("status") == "blocked":
                report["jobs"].append(details)
                continue
            remaining = int(budget - (time.monotonic() - started))
            if remaining < 1:
                report["jobs"].append({**details, "status": "timeout", "reason": "Overall validation time limit reached"})
                continue
            _public_copy(plan["dependency_input"])
            job_settings = {**settings, "job_timeout_seconds": min(remaining, int(settings.get("job_timeout_seconds", 1200)))}
            result = run_sandbox(plan["source"], plan["dependency_input"], plan["stages"],
                                 plan["image"], job_settings, log)
            for stage_number, stage in enumerate(result["stages"]):
                output = stage.pop("output", "")
                destination = work / "validation-logs" / f"{index}-{stage_number}.log"
                destination.parent.mkdir(exist_ok=True)
                destination.write_text(output)
                stage["log"] = str(destination.relative_to(work))
                stage["output_tail"] = output[-6000:]
            report["jobs"].append({**details, **result})
        states = {job["status"] for job in report["jobs"]}
        report.update(status=next((state for state in ("timeout", "failed", "blocked") if state in states),
                                  "passed" if states else "not_applicable"), reason="")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        report.update(status="blocked", reason=f"Validation setup could not complete: {error}")
        log.warning("%s", report["reason"])
    return report


def validation_summary(report):
    if not report or not report.get("enabled"):
        return "Static review; builds/tests were not run."
    parts = [f"Build/test validation: {report['status']}."]
    for job in report.get("jobs", ()):
        stages = ", ".join(f"{stage['name']} {stage['status']}" for stage in job.get("stages", ()))
        parts.append(f"{job['name']}: {stages or job.get('reason') or job['status']}.")
        if job.get("dependency_resolution", "").startswith("unlocked"):
            parts.append(f"Dependency resolution: {job['dependency_resolution']}.")
    if report.get("reason"):
        parts.append(report["reason"])
    return " ".join(parts)[:6000]
