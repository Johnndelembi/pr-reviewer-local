"""Load cited companion commits and PR heads without changing pinned snapshots."""
from pathlib import PurePosixPath
import re
import shutil


SHA = re.compile(r"(?<![A-Za-z0-9])[a-fA-F0-9]{7,40}(?![A-Za-z0-9])")
GITHUB = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(commit|pull)/([A-Za-z0-9]+)")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PULL_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)#([1-9][0-9]*)\b")


def resolve_companion_pull(reference, *, api):
    repository, number = reference["repository"], reference["number"]
    if (not REPOSITORY.fullmatch(repository)
            or any(part in (".", "..") for part in repository.split("/"))
            or type(number) is not int or number < 1):
        raise ValueError("Invalid companion PR reference")
    sha = api(f"repos/{repository}/pulls/{number}")["head"]["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid companion PR head SHA")
    return {"repository": repository, "number": number, "sha": sha}


def _trusted_repositories(sources, extra):
    trusted = {}
    for repository in [*sources.values(), *extra]:
        if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
                or any(part in (".", "..") for part in repository.split("/"))):
            raise ValueError("Invalid companion context repository")
        trusted[repository.casefold()] = repository
    return trusted


def _aliases(trusted):
    names = {}
    for repository in trusted.values():
        basename = repository.rsplit("/", 1)[-1]
        words = basename.removeprefix("notify-").removesuffix("-service").split("-")
        aliases = {repository, basename, " ".join(words)}
        if words[-1] in ("engine", "bridge"):
            aliases.add(words[-1])
        for alias in aliases:
            names.setdefault(alias.casefold(), set()).add(repository)
    return {alias: next(iter(repositories)) for alias, repositories in names.items()
            if len(repositories) == 1}


def _pull_citations(texts, trusted):
    aliases = _aliases(trusted)
    references = {}
    for source, text in texts:
        matches = [(m.start(), m[1].casefold(), m[3], m[0])
                   for m in GITHUB.finditer(text)
                   if m[2] == "pull" and m[3].isdigit() and int(m[3]) > 0]
        matches += [(m.start(), m[1].casefold(), m[2], m[0])
                    for m in PULL_REFERENCE.finditer(text)]
        for offset, name, number, citation in matches:
            repository = aliases.get(name)
            if repository:
                key = (repository, int(number))
                references.setdefault(key, []).append(
                    {"source": source, "line": text.count("\n", 0, offset) + 1,
                     "citation": citation, "kind": "pull_request_head"})
    if len(references) > 8:
        raise ValueError("More than eight companion PR references; split this review")
    return references


def _citations(texts, trusted):
    aliases = _aliases(trusted)
    alias_pattern = re.compile(
        r"(?<![A-Za-z0-9_/-])(" + "|".join(
            re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
        + r")(?![A-Za-z0-9_/-])", re.IGNORECASE) if aliases else None
    pull_repositories = {}
    for _, value in texts:
        for match in GITHUB.finditer(value):
            if match[2] == "pull" and match[1].casefold() in trusted:
                pull_repositories.setdefault(match[3], set()).add(trusted[match[1].casefold()])
    candidates, unavailable, evidence = {}, [], []
    for source, text in texts:
        offset = 0
        for paragraph in re.split(r"\n\s*\n", text):
            start = text.find(paragraph, offset)
            offset = start + len(paragraph)
            line = text.count("\n", 0, start) + 1
            mentions = []
            urls = list(GITHUB.finditer(paragraph))
            for match in urls:
                repository = trusted.get(match[1].casefold())
                if repository:
                    mentions.append((match.start(), repository))
                elif match[2] == "commit":
                    unavailable.append({"source": source, "line": line,
                                        "citation": match[0], "reason": "repository is not approved"})
            if alias_pattern:
                mentions.extend((match.start(), aliases[match[0].casefold()])
                                for match in alias_pattern.finditer(paragraph))
            # An unapproved repository name must stop attribution to an earlier alias.
            for match in re.finditer(
                    r"(?<![A-Za-z0-9_/-])(?:[A-Za-z0-9_.-]+/)?notify-[A-Za-z0-9_.-]+"
                    r"(?![A-Za-z0-9_/-])", paragraph):
                mentions.append((match.start(), aliases.get(match[0].casefold())))
            for match in re.finditer(r"\bPR\s*#(\d+)\b", paragraph, re.IGNORECASE):
                repositories = pull_repositories.get(match[1], set())
                if len(repositories) == 1:
                    mentions.append((match.start(), next(iter(repositories))))
            mentions.sort(key=lambda item: item[0])
            for match in SHA.finditer(paragraph):
                # A URL identifies its own repository, even if an alias appeared earlier.
                url = next((url for url in urls
                            if url.start() <= match.start() < url.end()), None)
                if len(match[0]) < 40 and match[0].isdigit() and not (url and url[2] == "commit"):
                    continue
                if url:
                    repository = trusted.get(url[1].casefold()) if url[2] == "commit" else None
                else:
                    line_start = paragraph.rfind("\n", 0, match.start()) + 1
                    line_text = paragraph[line_start:]
                    owner = re.match(r"\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?)?([^:\n]+):", line_text)
                    owner_match = alias_pattern.fullmatch(owner[1].strip()) if owner and alias_pattern else None
                    preceding = [item for item in mentions if item[0] < match.start()]
                    unique = {item[1] for item in mentions}
                    repository = aliases[owner_match[0].casefold()] if owner_match else (
                        preceding[-1][1] if preceding else (
                            next(iter(unique)) if len(unique) == 1 else None))
                if not repository:
                    if not url and preceding and preceding[-1][1] is None:
                        unavailable.append({"source": source,
                                            "line": line + paragraph.count("\n", 0, match.start()),
                                            "citation": paragraph.strip(), "ref": match[0],
                                            "reason": "repository reference is not approved"})
                    continue
                provenance = {"source": source, "line": line + paragraph.count("\n", 0, match.start()),
                              "citation": paragraph.strip()}
                key = (repository, match[0].lower())
                if key not in candidates and len(candidates) >= 32:
                    raise ValueError("More than 32 companion commit references; split this review")
                candidates.setdefault(key, []).append(provenance)
            for index, value in enumerate(paragraph.splitlines()):
                if re.search(r"mock[- ]provider|harness", value, re.IGNORECASE):
                    evidence.append({"source": source, "line": line + index, "citation": value.strip(),
                                     "status": "author_reported", "author_reported": True,
                                     "execution_verification": "not_independently_verified",
                                     "artifact_collection": "not_performed_by_this_loader",
                                     "reason": "A textual reference does not establish an execution result "
                                               "or an acceptance gate; assess the source's stated scope"})
    return candidates, unavailable, evidence


def collect_companion_context(work, changed_files, pr, *, sources, api, gh,
                              extract_snapshot, log, context_repositories=(), comments=()):
    """Resolve explicit references in trusted repos and download exact snapshots."""
    trusted = _trusted_repositories(sources, context_repositories)
    texts = [("PR description", pr.get("body") or "")]
    total = len(texts[0][1].encode())
    for comment in comments:
        body = comment.get("body") or ""
        size = len(body.encode())
        total += size
        if size > 1_000_000 or total > 5_000_000:
            raise ValueError("Companion comment citations exceed the 5 MB / 1 MB per-comment limit")
        texts.append((f"PR comment {comment.get('html_url') or comment.get('id')}", body))
    for item in changed_files:
        path = item["filename"] if isinstance(item, dict) else item
        relative = PurePosixPath(path)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in path
                or relative.suffix.lower() != ".md"):
            continue
        target = work / "head" / path
        if not target.is_file():
            continue
        size = target.stat().st_size
        total += size
        if size > 1_000_000 or total > 5_000_000:
            raise ValueError("Companion citation documents exceed the 5 MB / 1 MB per-file limit")
        texts.append((f"head/{path}", target.read_text(errors="replace")))
    candidates, unavailable, evidence = _citations(texts, trusted)
    pull_requests = []
    for (repository, number), provenance in _pull_citations(texts, trusted).items():
        reference = {"repository": repository, "number": number, "sha": None}
        try:
            reference = resolve_companion_pull(reference, api=api)
            candidates.setdefault((repository, reference["sha"]), []).extend(provenance)
        except Exception as error:
            unavailable.append({**reference, "provenance": provenance, "reason": str(error)[:300]})
            log.warning("Companion PR unavailable %s#%s: %s", repository, number, error)
        pull_requests.append(reference)
    snapshots = {}
    resolved = {}
    for (repository, ref), provenance in candidates.items():
        try:
            sha = ref
            if len(ref) != 40:
                sha = api(f"repos/{repository}/commits/{ref}")["sha"].lower()
                if not sha.startswith(ref):
                    raise ValueError("Resolved companion commit does not match its citation")
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise ValueError("Invalid companion commit SHA")
        except Exception as error:
            unavailable.append({"repository": repository, "ref": ref, "provenance": provenance,
                                "reason": str(error)[:300]})
            log.warning("Companion context unavailable %s@%s: %s", repository, ref, error)
            continue
        resolved.setdefault((repository, sha), []).extend(provenance)
    if len(resolved) > 8:
        raise ValueError("More than eight companion commit snapshots; split this review")
    for (repository, sha), provenance in resolved.items():
        relative = PurePosixPath("supplemental", repository.rsplit("/", 1)[-1], sha)
        if str(relative) in snapshots:
            raise ValueError("Companion repositories have colliding snapshot paths")
        created = False
        target = work.joinpath(*relative.parts)
        try:
            data = gh("api", "--hostname", "github.com", f"repos/{repository}/tarball/{sha}")
            target.mkdir(parents=True, exist_ok=False)
            created = True
            omissions = extract_snapshot(data, target)
        except Exception as error:
            if created:
                shutil.rmtree(target)
            unavailable.append({"repository": repository, "sha": sha, "provenance": provenance,
                                "reason": str(error)[:300]})
            log.warning("Companion context unavailable %s@%s: %s", repository, sha, error)
            continue
        snapshots[str(relative)] = {"repository": repository, "sha": sha, "path": str(relative),
                                    "author_reported": True, "supplemental": True, "pinned": False,
                                    "provenance": provenance, "omitted_links": omissions}
        log.info("Companion context included %s@%s separately from pinned source", repository, sha)
    return {"snapshots": list(snapshots.values()), "unavailable": unavailable, "evidence": evidence,
            "pull_requests": pull_requests,
            "trust": "base-approved repositories plus explicitly configured source repositories"}
