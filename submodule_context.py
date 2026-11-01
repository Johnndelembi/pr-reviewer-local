"""Load affected submodules at their exact gitlinks, trusting only base URLs."""
import configparser
from pathlib import PurePosixPath
import posixpath
import re
import shutil


def submodule_sources(root):
    config = configparser.RawConfigParser()
    config.read(root / ".gitmodules")
    sources = {}
    for section in config.sections():
        path = config.get(section, "path", fallback="")
        relative = PurePosixPath(path)
        if (not path or relative.is_absolute() or ".." in relative.parts
                or str(relative) == "." or "\\" in path):
            raise ValueError("Unsafe submodule path")
        match = re.fullmatch(
            r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
            config.get(section, "url", fallback=""))
        if match:
            sources[path] = match.group(1)
    return sources


def _compose(path):
    name = PurePosixPath(path).name.lower()
    return "compose" in name and name.endswith((".yml", ".yaml"))


def _group_config(path):
    name = PurePosixPath(path).name.lower()
    return (_compose(path) or name == ".env" or name.startswith(".env.")
            or name.endswith(".env") or ".env." in name)


def _sibling_compose_paths(snapshot, parent_path):
    if parent_path.is_absolute() or ".." in parent_path.parts:
        return []
    parent = snapshot / str(parent_path)
    if not parent.is_dir():
        return []
    siblings = []
    for candidate in parent.iterdir():
        if candidate.is_file() and _compose(candidate.name):
            siblings.append(str(parent_path / candidate.name))
            if len(siblings) > 16:
                raise ValueError("More than sixteen Compose files in one service group")
    return sorted(siblings)


def _related_compose_paths(snapshot, changed):
    if _compose(changed):
        return [changed]
    return (_sibling_compose_paths(snapshot, PurePosixPath(changed).parent)
            if _group_config(changed) else [])


def _build_contexts(snapshot, path):
    """Recognize literal Compose build/context lines, without evaluating YAML."""
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        return []
    target = snapshot / path
    if not target.is_file() or target.stat().st_size > 1_000_000:
        return []
    contexts, additional_indent = [], None
    for line in target.read_text(errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if additional_indent is not None and indent <= additional_indent:
            additional_indent = None
        if re.match(r"^\s*additional_contexts:\s*(?:#.*)?$", line):
            additional_indent = indent
            continue
        pattern = (r"^\s*(?:[A-Za-z0-9_.-]+:\s*|-\s*[A-Za-z0-9_.-]+=)([^#]*?)(?:\s+#.*)?$"
                   if additional_indent is not None else r"^\s*(?:context|build):\s*([^#]*?)(?:\s+#.*)?$")
        match = re.match(pattern, line)
        if not match:
            continue
        value = match.group(1).strip().strip("\"'")
        if (not value or re.search(r"[^A-Za-z0-9_./-]", value)
                or value.startswith(("/", "-"))):
            continue
        context = posixpath.normpath(posixpath.join(str(relative.parent), value))
        if context != ".." and not context.startswith("../"):
            contexts.append(context)
    return contexts


def select_submodules(base_sources, head_sources, files, diff, snapshots=None):
    """Return side-specific trusted paths, selection reasons, and skipped URLs."""
    trusted = {source.casefold(): source for source in base_sources.values()}
    sources = {"base": base_sources, "head": {}}
    skipped = []
    for path, source in head_sources.items():
        if source.casefold() in trusted:
            sources["head"][path] = trusted[source.casefold()]
        else:
            skipped.append(f"head/{path}: repository {source} is not trusted by base")
    text = diff.decode(errors="replace") if isinstance(diff, bytes) else diff
    reasons = {}

    def include(source, reason):
        values = reasons.setdefault(source.casefold(), [])
        if reason not in values:
            values.append(reason)

    paths = {"base": [], "head": []}
    for item in files:
        paths["head"].append(item["filename"])
        paths["base"].append(item.get("previous_filename", item["filename"]))
    for side, mapping in sources.items():
        for path, source in mapping.items():
            if path in text or source.rsplit("/", 1)[-1] in text:
                include(source, "explicit diff reference")
            for changed in paths[side]:
                if changed == path or changed.startswith(path + "/"):
                    include(source, f"changed path {changed}")
                parent = str(PurePosixPath(changed).parent)
                if _group_config(changed) and parent != "." and path.startswith(parent + "/"):
                    include(source, f"service group {changed}")
        if snapshots:
            compose_paths = {compose for changed in paths[side]
                             for compose in _related_compose_paths(snapshots[side], changed)}
            # A docs reference can select a service whose unchanged Compose names shared dependencies.
            compose_paths.update(compose for path, source in mapping.items()
                                 if source.casefold() in reasons
                                 for compose in _sibling_compose_paths(snapshots[side], PurePosixPath(path).parent))
            for compose in sorted(compose_paths):
                for context in _build_contexts(snapshots[side], compose):
                    if context == ".":
                        skipped.append(f"{side}/{compose}: repository-wide build context '.' needs explicit service references")
                        continue
                    for path, source in mapping.items():
                        if (context == path or context.startswith(path + "/")
                                or path.startswith(context + "/")):
                            include(source, f"build context {context} in {compose}")
    if len(reasons) > 8:
        raise ValueError("More than eight referenced submodule repositories; split this review")
    selected = {side: {path: source for path, source in mapping.items()
                       if source.casefold() in reasons} for side, mapping in sources.items()}
    return selected, reasons, skipped


def expand_referenced_submodules(work, repo, pr, diff, files, *, api, gh, extract_snapshot, log):
    snapshots = {side: work / side for side in ("base", "head")}
    selected, reasons, skipped = select_submodules(
        submodule_sources(snapshots["base"]), submodule_sources(snapshots["head"]),
        files, diff, snapshots)
    for reason in skipped:
        log.warning("Submodule context skipped %s", reason)
    records, cached = {}, {}
    for side in ("head", "base"):
        if not selected[side]:
            continue
        tree = api(f"repos/{repo}/git/trees/{pr[side]['sha']}?recursive=1")
        if tree.get("truncated"):
            raise ValueError("Submodule tree is incomplete")
        links = {entry["path"]: entry["sha"] for entry in tree["tree"] if entry["mode"] == "160000"}
        for path, source in selected[side].items():
            if path not in links:
                log.info("Submodule context skipped %s/%s: no gitlink in this revision", side, path)
                continue
            sha = links[path]
            if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                raise ValueError("Invalid submodule gitlink SHA")
            target = snapshots[side] / path
            key = (source.casefold(), sha)
            if key in cached:
                cached_path, omissions = cached[key]
                shutil.copytree(cached_path, target, dirs_exist_ok=True)
            else:
                data = gh("api", "--hostname", "github.com", f"repos/{source}/tarball/{sha}")
                target.mkdir(parents=True, exist_ok=True)
                omissions = extract_snapshot(data, target)
                cached[key] = (target, omissions)
            why = reasons[source.casefold()]
            records[f"{side}/{path}"] = {"repository": source, "sha": sha,
                                        "omitted_links": omissions, "selection_reasons": why}
            log.info("Submodule context included %s/%s at %s: %s", side, path, sha, "; ".join(why))
    return records
