"""Build bounded validation plans from snapshots without executing repository code."""
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tomllib


class UnsupportedValidation(ValueError):
    pass


def _relative(path):
    path = PurePosixPath(path)
    if path.is_absolute() or ".." in path.parts or "\\" in str(path):
        raise UnsupportedValidation("Unsafe validation target path")
    return str(path)


def _manifest(path):
    if path.stat().st_size > 1_000_000:
        raise UnsupportedValidation("Package manifest exceeds 1 MB")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise UnsupportedValidation("Package manifest must be an object")
    return value


def _source_change(name):
    path = PurePosixPath(name)
    return not (str(path).startswith(("docs/", ".agents/", "ai/workflow-contract/"))
                or path.suffix.lower() in (".md", ".mdx", ".rst"))


def discover_targets(work, repo, context):
    """Choose changed executable packages; companion evidence is never a test target."""
    head = Path(work) / "head"
    changed = [item["filename"] for item in context.get("files", ())]
    if not any(_source_change(name) for name in changed):
        return []
    if (head / "pyproject.toml").is_file():
        return ["."]
    if (head / "package.json").is_file():
        scripts = _manifest(head / "package.json").get("scripts", {})
        if "build" in scripts or "test" in scripts:
            return ["."]
    targets = []
    for key, entry in context.get("expanded_submodules", {}).items():
        if not key.startswith("head/"):
            continue
        path = _relative(key[5:])
        explicitly_changed = any(name == path or name.startswith(path + "/") for name in changed)
        selected = explicitly_changed
        selected |= any(reason.startswith(("service group ", "build context "))
                        for reason in entry.get("selection_reasons", ()))
        directory = head / path
        if ((directory / "package.json").is_file() and not explicitly_changed
                and _manifest(directory / "package.json").get("name") == "nest-shared"):
            continue
        if selected and ((directory / "package.json").is_file()
                         or (directory / "pyproject.toml").is_file()):
            targets.append(path)
    # nest-shared is compiled as a dependency, unless it is itself the changed target.
    return sorted(set(targets))


def _stage(name, argv, cwd, network=False):
    return {"name": name, "argv": argv, "cwd": cwd, "network": network}


def _safe_dependencies(data):
    result = {"name": data.get("name", "review-validation"),
              "version": data.get("version", "0.0.0"), "private": True}
    if (not isinstance(result["name"], str)
            or not re.fullmatch(r"(?:@[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*", result["name"])
            or not isinstance(result["version"], str) or len(result["version"]) > 128):
        raise UnsupportedValidation("Invalid package name or version")
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = data.get(field, {})
        if not isinstance(values, dict):
            raise UnsupportedValidation(f"Invalid {field}")
        safe = {}
        for name, spec in values.items():
            if (not isinstance(name, str)
                    or not re.fullmatch(r"(?:@[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*", name)
                    or not isinstance(spec, str)):
                raise UnsupportedValidation("Invalid dependency name or version")
            if spec.startswith("workspace:"):
                if not re.fullmatch(r"workspace:[A-Za-z0-9*^~<>=|.,+ _-]+", spec):
                    raise UnsupportedValidation(f"Unsupported workspace dependency {name}")
            elif spec.startswith("link:"):
                # The caller checks this against an exact sibling snapshot.
                if "\\" in spec or PurePosixPath(spec[5:]).is_absolute():
                    raise UnsupportedValidation(f"Unsafe linked dependency {name}")
            elif not re.fullmatch(r"[A-Za-z0-9*^~<>=|.,+ _-]+", spec):
                raise UnsupportedValidation(f"Non-registry dependency {name} is unsupported")
            safe[name] = spec
        if safe:
            result[field] = safe
    return result


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _node_plan(head, relative, destination):
    target = head / relative
    data = _manifest(target / "package.json")
    scripts = data.get("scripts", {})
    if (not isinstance(scripts, dict) or not any(name in scripts for name in ("build", "test"))
            or any(not isinstance(scripts[name], str) for name in ("build", "test") if name in scripts)):
        raise UnsupportedValidation("No supported configured build or test command")
    packages, queue, dependencies = {}, [relative], []
    candidates = {}
    for manifest in head.rglob("package.json"):
        if "node_modules" in manifest.parts or len(manifest.relative_to(head).parts) > 8:
            continue
        candidate = _manifest(manifest)
        if candidate.get("name"):
            candidates.setdefault(candidate["name"], []).append(str(manifest.parent.relative_to(head)))
        if sum(len(values) for values in candidates.values()) > 128:
            raise UnsupportedValidation("More than 128 package manifests")
    while queue:
        package = queue.pop(0)
        if package in packages:
            continue
        if len(packages) >= 8:
            raise UnsupportedValidation("More than eight local workspace dependencies")
        safe = _safe_dependencies(_manifest(head / package / "package.json"))
        packages[package] = safe
        for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for name, spec in safe.get(field, {}).items():
                if not spec.startswith(("workspace:", "link:")):
                    continue
                matches = candidates.get(name, [])
                if spec.startswith("link:"):
                    resolved = (head / package / spec[5:]).resolve()
                    if not resolved.is_relative_to(head.resolve()):
                        raise UnsupportedValidation(f"Linked dependency {name} escapes snapshot")
                    matches = [item for item in matches if (head / item).resolve() == resolved]
                if len(matches) != 1:
                    raise UnsupportedValidation(f"Missing or ambiguous exact snapshot for local dependency {name}")
                queue.append(matches[0])
                if matches[0] != relative and matches[0] not in dependencies:
                    dependencies.append(matches[0])
    for path, manifest in packages.items():
        _write_json(destination / path / "package.json", manifest)
    if "." not in packages:
        _write_json(destination / "package.json", {"name": "review-validation", "private": True})
    (destination / "pnpm-workspace.yaml").write_text(
        "packages:\n" + "".join(f"  - {json.dumps(path)}\n" for path in sorted(packages))
        + "onlyBuiltDependencies: []\n")
    # A standalone lock has matching importer paths; synthetic workspaces do not.
    frozen = relative == "." and len(packages) == 1 and (target / "pnpm-lock.yaml").is_file()
    if frozen:
        shutil.copyfile(target / "pnpm-lock.yaml", destination / "pnpm-lock.yaml")
    stages = [_stage("install", ["pnpm", "install", "--frozen-lockfile" if frozen else "--no-frozen-lockfile",
                                  "--ignore-scripts", "--ignore-pnpmfile",
                                  "--config.manage-package-manager-versions=false",
                                  "--config.package-manager-strict=false"], "/workspace", True)]
    for dependency in dependencies:
        manifest = packages[dependency]
        if manifest["name"] == "nest-shared":
            stages.append(_stage("build dependency nest-shared",
                                 ["pnpm", "exec", "tsc", "-p", "tsconfig.build.json"],
                                 "/workspace/" + dependency))
    cwd = "/workspace" if relative == "." else "/workspace/" + relative
    bridge_make = (target / "Makefile").is_file() and data.get("name") == "notify-voice-ai-bridge"
    aliases = {"notify-" + alias + "-service": alias for alias in
               ("auth", "users", "media", "payments", "audit", "operations", "messaging",
                "contacts", "campaigns", "webhooks", "waba", "workflow")}
    root_make = (relative != "." and (head / "Makefile").is_file()
                 and (head / "package.json").is_file()
                 and _manifest(head / "package.json").get("name") == "notify-backend"
                 and data.get("name") in aliases)
    for name in ("build", "test"):
        if name not in scripts:
            continue
        jest = name == "test" and re.fullmatch(r"jest(?:\s+--[^;&|]+)?", scripts[name].strip())
        # Parent Make test cannot forward Jest's worker bound to the test script.
        argv = (["pnpm", "run", name, "--runInBand"] if jest else
                ["make", name, "s=" + aliases[data["name"]]] if root_make else
                ["make", name, "PNPM=pnpm"] if bridge_make else ["pnpm", "run", name])
        stages.append(_stage(name, argv, "/workspace" if root_make and not jest else cwd))
    return {"name": data.get("name", relative), "kind": "node", "stages": stages,
            "dependency_resolution": "locked" if frozen else "unlocked synthetic workspace",
            "local_dependencies": dependencies}


def _python_requirement(value):
    if (not isinstance(value, str) or len(value) > 1000 or "@" in value or "://" in value
            or "\\" in value or "\n" in value or "\r" in value or re.search(r"\s--", value)
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:\s*[<>=!~; ].*)?$", value)):
        raise UnsupportedValidation("Non-registry Python requirement is unsupported")
    return value


def _python_plan(head, relative, destination):
    target = head / relative
    project_data = tomllib.loads((target / "pyproject.toml").read_text())
    project = project_data.get("project", {})
    requirements = list(project.get("dependencies", ()))
    requirements += project.get("optional-dependencies", {}).get("dev", ())
    build_requirements = list(project_data.get("build-system", {}).get("requires", ("setuptools", "wheel")))
    for value in requirements + build_requirements:
        _python_requirement(value)
    cwd = "/workspace" if relative == "." else "/workspace/" + relative
    venv = cwd + "/.venv"
    inputs = destination / relative
    inputs.mkdir(parents=True, exist_ok=True)
    frozen = (target / "uv.lock").is_file()
    if frozen:
        lock = tomllib.loads((target / "uv.lock").read_text())
        for package in lock.get("package", ()):
            source = package.get("source", {})
            if source and source != {"editable": "."} and set(source) != {"registry"}:
                raise UnsupportedValidation("Non-registry dependency source in uv.lock")
        # JSON strings are valid TOML strings. Preserve only static project metadata.
        lines = ["[project]", "name = " + json.dumps(project.get("name", "review-validation")),
                 "version = " + json.dumps(project.get("version", "0.0.0")),
                 "requires-python = " + json.dumps(project.get("requires-python", ">=3.12")),
                 "dependencies = " + json.dumps(project.get("dependencies", [])),
                 "[project.optional-dependencies]",
                 "dev = " + json.dumps(project.get("optional-dependencies", {}).get("dev", []))]
        (inputs / "pyproject.toml").write_text("\n".join(lines) + "\n")
        shutil.copyfile(target / "uv.lock", inputs / "uv.lock")
        stages = [_stage("install locked dependencies", ["uv", "sync", "--frozen", "--extra", "dev",
                                                          "--no-install-project", "--no-build",
                                                          "--no-managed-python"], cwd, True)]
    else:
        stages = [_stage("create environment", ["uv", "venv", venv, "--python", "3.12",
                                                "--no-managed-python"], cwd, True)]
    extra = build_requirements + ["build", "pytest", "editables"]
    if not frozen:
        extra = requirements + extra
    (inputs / "validation-requirements.txt").write_text("\n".join(extra) + "\n")
    stages.append(_stage("install binary build dependencies",
                         ["uv", "pip", "install", "--python", venv + "/bin/python", "--only-binary", ":all:",
                          "-r", cwd + "/validation-requirements.txt"], cwd, True))
    # Source archives can include this runner-owned cache, duplicating installed dependencies.
    stages.append(_stage("clear dependency cache", ["uv", "cache", "clean"], cwd))
    stages.append(_stage("build", [venv + "/bin/python", "-m", "build", "--no-isolation"], cwd))
    stages.append(_stage("install project", ["uv", "pip", "install", "--python", venv + "/bin/python",
                                             "--offline", "--no-deps", "--no-build-isolation", "-e", "."], cwd))
    # AI's Make test prerequisite only observes the existing venv; its build target uses Docker.
    if project.get("name") == "notify-ai-engine" and (target / "Makefile").is_file():
        test = ["make", "test", "VENV=" + venv]
    else:
        test = [venv + "/bin/python", "-m", "pytest"]
    stages.append(_stage("test", test, cwd))
    return {"name": project.get("name", relative), "kind": "python", "stages": stages,
            "dependency_resolution": "locked" if frozen else "unlocked Python requirements"}


def prepare_target(work, target_path, dependency_input, settings):
    relative = _relative(target_path)
    head = Path(work) / "head"
    destination = Path(dependency_input)
    destination.mkdir(parents=True, exist_ok=True)
    plan = {"name": relative, "path": relative, "source": head, "dependency_input": destination}
    try:
        if (head / relative / "package.json").is_file():
            plan.update(_node_plan(head, relative, destination))
        elif (head / relative / "pyproject.toml").is_file():
            plan.update(_python_plan(head, relative, destination))
        else:
            raise UnsupportedValidation("No supported package manifest")
        plan["image"] = settings.get(plan["kind"] + "_image")
    except (UnsupportedValidation, ValueError, TypeError, OSError) as exc:
        plan.update(status="blocked", reason=str(exc), stages=[])
    return plan


def plan_checks(work, repo, context, settings):
    targets = discover_targets(work, repo, context)
    maximum = max(1, min(int(settings.get("max_targets", 4)), 8))
    plans = [prepare_target(work, path, Path(work) / "validation-inputs" / str(index), settings)
             for index, path in enumerate(targets[:maximum])]
    if len(targets) > maximum:
        plans.append({"name": "additional targets", "status": "blocked", "stages": [],
                      "reason": f"Target limit {maximum}; skipped: {', '.join(targets[maximum:])}"})
    return plans
