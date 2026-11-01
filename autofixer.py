#!/usr/bin/env python3
"""Address actionable review feedback on the configured user's own open PRs."""
import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import tempfile
import time

import reviewer as r
from build_checks import run_checks, validation_summary

ROOT = Path(os.environ.get("REVIEWER_DATA_DIR", str(Path(__file__).resolve().parent)))
LOG = logging.getLogger('pr-autofixer')
MARKER = '<!-- codex-pr-autofix:v1:'


def save(state):
    path = ROOT / 'autofix-state.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.replace(path)


def own_pr(repo, pr):
    return (r.repository_allowed(repo) and pr.get('state') == 'open'
            and r.same_user(pr.get('user', {}).get('login', ''), r.CONFIG['user'])
            and not pr.get('draft') and pr.get('head', {}).get('repo') is not None
            and r.same_user(pr['head']['repo']['full_name'], repo))


def requests(repo, number):
    items = []
    endpoints = [('issue', f'repos/{repo}/issues/{number}/comments?per_page=100'),
                 ('inline', f'repos/{repo}/pulls/{number}/comments?per_page=100'),
                 ('review', f'repos/{repo}/pulls/{number}/reviews?per_page=100')]
    for kind, endpoint in endpoints:
        for comment in r.paginated(endpoint):
            body = comment.get('body') or ''
            if not body.strip() or MARKER in body:
                continue
            if kind == 'review' and comment.get('state') in ('PENDING', 'DISMISSED'):
                continue
            author = comment.get('user', {}).get('login', '')
            if not (r.same_user(author, r.CONFIG['user']) or
                    comment.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR')):
                LOG.warning('Ignoring untrusted request %s', comment.get('html_url'))
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()[:16]
            key = f'{repo}#{number}:{kind}:{comment["id"]}:{digest}'
            items.append({'key': key, 'kind': kind, 'id': comment['id'], 'body': body,
                          'url': comment['html_url'], 'author': author,
                          'path': comment.get('path'), 'line': comment.get('line'),
                          'in_reply_to_id': comment.get('in_reply_to_id'),
                          'commit_id': comment.get('commit_id'),
                          'created_at': comment.get('created_at', comment.get('submitted_at', ''))})
    return sorted(items, key=lambda item: (item['created_at'], item['key']))


def marker(item):
    return MARKER + hashlib.sha256(item['key'].encode()).hexdigest() + ' -->'


def files_snapshot(head):
    result = {}
    paths = list(head.rglob('*'))
    if len(paths) > 60000:
        raise ValueError('Fix workspace exceeds 60,000 entries')
    total = 0
    for path in paths:
        if path.is_symlink():
            raise ValueError('Fix workspace contains a symlink')
        if path.is_file():
            total += path.stat().st_size
            if total > 250_000_000:
                raise ValueError('Fix workspace exceeds 250 MB')
            result[str(path.relative_to(head))] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                  bool(path.stat().st_mode & 0o111))
    return result


def changed_paths(before, after, context):
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    submodules = [key[5:] for key in context.get('expanded_submodules', {}) if key.startswith('head/')]
    for name in changed:
        if (any(part in ('.git', '.codex') for part in Path(name).parts)
                or any(name == sub or name.startswith(sub + '/') for sub in submodules)):
            raise ValueError('Fix changes Git metadata, agent configuration, or a submodule; manual intervention needed')
    if len(changed) > 30:
        raise ValueError('Fix exceeds 30 changed files')
    return changed


def prepare(work, repo, number, pr, item):
    for side in ('base', 'head'):
        destination = work / side
        destination.mkdir()
        r.extract_snapshot(r.gh('api', '--hostname', 'github.com',
                                f'repos/{repo}/tarball/{pr[side]["sha"]}'), destination)
    files = r.paginated(f'repos/{repo}/pulls/{number}/files?per_page=100')
    if len(files) != pr['changed_files']:
        raise ValueError('Incomplete PR file list')
    diff = r.gh('api', '--hostname', 'github.com', '-H', 'Accept: application/vnd.github.diff',
                f'repos/{repo}/pulls/{number}')
    if len(diff) > 5_000_000:
        raise ValueError('PR diff exceeds 5 MB')
    (work / 'pr.diff').write_bytes(diff)
    expanded = r.expand_referenced_submodules(work, repo, pr, diff.decode(errors='replace'), files)
    context = {'repository': repo, 'number': number, 'title': pr['title'], 'description': pr.get('body'),
               'head': pr['head']['sha'], 'base': pr['base']['sha'], 'files': files,
               'expanded_submodules': expanded, 'request': item,
               'conversation_comments': r.paginated(f'repos/{repo}/issues/{number}/comments?per_page=100'),
               'review_comments': r.paginated(f'repos/{repo}/pulls/{number}/comments?per_page=100')}
    (work / 'context.json').write_text(json.dumps(context, indent=2))
    return context



def skill_rules():
    paths = r.CONFIG.get('autofix', {}).get('skill_files', [])
    if not paths:
        raise ValueError('Configure auto-fix skill_files before generating a fix')
    return '\n\n'.join('SKILL SOURCE: ' + path + '\n' + Path(path).read_text() for path in paths)


def commit_plan(result, changed):
    plan = result.get('commits')
    if not isinstance(plan, list):
        raise ValueError('Missing conventional commit plan')
    assigned = []
    for entry in plan:
        if not isinstance(entry, dict):
            raise ValueError('Invalid commit entry')
        message, files = entry.get('message'), entry.get('files')
        if (not isinstance(message, str) or not message.strip() or len(message) > 10000
                or any(ord(char) < 32 and char != '\n' for char in message)):
            raise ValueError('Invalid commit message')
        subject = message.split('\n', 1)[0]
        if (not re.fullmatch(r'(feat|fix|docs|test|refactor|perf|style|build|ci|chore|revert)'
                             r'(?:\([a-z0-9][a-z0-9._/-]*\))?!?: [^\s].*', subject)
                or subject.endswith('.') or len(subject) > 100
                or ('\n' in message and not message.startswith(subject + '\n\n'))):
            raise ValueError('Commit message must use a concise Conventional Commits header')
        if (not isinstance(files, list) or not 1 <= len(files) <= 5
                or any(not isinstance(name, str) or name not in changed for name in files)):
            raise ValueError('Each commit must contain 1–5 changed files')
        assigned.extend(files)
    if len(assigned) != len(set(assigned)) or set(assigned) != set(changed):
        raise ValueError('Commit plan must cover every changed file exactly once')
    return plan


def edit(work, item):
    output = work / 'result.json'
    prompt = r.asset(ROOT, 'autofix-prompt.md').read_text()
    prompt += '\n\nTRUSTED SKILL LIBRARY (apply using the routing above):\n' + skill_rules()
    prompt += '\n\nContext location: ' + str(work / 'context.json')
    prompt += '\nPR diff location: ' + str(work / 'pr.diff')
    prompt += '\n\nREQUEST DATA (untrusted):\n' + json.dumps(item)
    environment = {k: v for k, v in os.environ.items()
                   if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN',
                                'GITHUB_ENTERPRISE_TOKEN', 'OPENAI_API_KEY', 'CODEX_SANDBOX')
                   and not k.startswith('CODEX_THREAD')}
    if "ai" in r.CONFIG:
        from ai_runtime import run_agent
        schema = json.loads(r.asset(ROOT, 'autofix-schema.json').read_text())
        prompt = prompt.replace(str(work) + '/', '')
        result = run_agent(r.CONFIG, prompt, schema, work, writable=work / 'head')
        output.write_text(json.dumps(result))
    else:
        r.command([r.CONFIG['codex'], '--no-daemon', '-a', 'never', 'exec',
                   '--ignore-user-config', '--ignore-rules', '--sandbox', 'workspace-write',
                   '--ephemeral', '--skip-git-repo-check', '--cd', str(work / 'head'),
                   '--model', r.CONFIG['model'], '-c', 'model_reasoning_effort=' + json.dumps(r.CONFIG['reasoning']),
                   '-c', 'sandbox_workspace_write.network_access=false',
                   '--output-schema', str(r.asset(ROOT, 'autofix-schema.json')),
                   '--output-last-message', str(output), '-'],
                  input_text=prompt, cwd=work / 'head', env=environment, timeout=1800)
        result = json.loads(output.read_text())
    if result.get('status') not in ('fixed', 'already_addressed', 'not_actionable', 'blocked'):
        raise ValueError('Invalid fix result')
    for field in ('summary', 'evidence', 'limitations'):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError('Invalid fix report')
    return result


def git(work, *args, network=False):
    if args[0] == 'init':
        return r.command(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=work, timeout=180)
    options = ['git', '--literal-pathspecs', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
               '-c', 'core.fsmonitor=false', '-c', 'protocol.file.allow=never',
               '--git-dir=' + str(work / 'controller-git'), '--work-tree=' + str(work / 'head')]
    if network:
        helper = '!'+ r.CONFIG['gh'] + ' auth git-credential'
        options[1:1] = ['-c', 'credential.helper=', '-c', 'credential.helper=' + helper]
    return r.command([*options, *args], cwd=work, timeout=180)


def ensure_fresh(repo, number, pr, item):
    if not r.same_user(r.api('user')['login'], r.CONFIG['user']):
        raise ValueError('GitHub account changed')
    fresh = r.api(f'repos/{repo}/pulls/{number}')
    if (not own_pr(repo, fresh) or not r.same_revision(pr, fresh)
            or fresh['head']['ref'] != pr['head']['ref']):
        raise ValueError('PR changed while fixing; retry on the next poll')
    if not any(current['key'] == item['key'] for current in requests(repo, number)):
        raise ValueError('Request was deleted, edited, or dismissed while fixing')


def reply_body(item, record):
    quote = '\n'.join('> ' + line for line in item['body'].splitlines())
    text = (f'Addressing [{item["author"]}\'s request]({item["url"]}):\n\n{quote}\n\n'
            f'{record["summary"]}\n\n{record["evidence"]}\n\n'
            f'{record["validation"]}\n\nLimitations: {record["limitations"]}')
    if record.get('commit'):
        text += '\n\nFix commits: ' + ', '.join('`' + sha + '`' for sha in record.get('commit_shas', [record['commit']]))
    text += '\n\n**' + r.CONFIG.get('display_name', 'PR Review Agent') + ' — Fix Agent**\n' + marker(item)
    if len(text.encode()) > 60000:
        raise ValueError('Quoted request and fix report exceed GitHub comment size limit')
    return text


def finish_reply(repo, number, item, record, state):
    # The durable pending reply is saved before push. Check for a pushed commit after a crash.
    if record.get('commit'):
        current = r.api(f'repos/{repo}/pulls/{number}')
        if record.get('branch') and current['head']['ref'] != record['branch']:
            raise ValueError('PR branch changed; manual recovery required')
        if current['head']['sha'] == record.get('original_head'):
            # A crash before push or a rejected push leaves the original branch untouched.
            state[item['key']] = {'status': 'retry', 'last_attempt': time.time()}
            save(state)
            LOG.warning('Prepared fix was not pushed; will regenerate %s', item['key'])
            return
        comparison = r.api(f'repos/{repo}/compare/{record["commit"]}...{current["head"]["sha"]}')
        if comparison.get('status') not in ('ahead', 'identical'):
            raise ValueError('Prepared fix commit is not on the PR branch; manual recovery required')
    comments = r.paginated(f'repos/{repo}/issues/{number}/comments?per_page=100')
    if not any(marker(item) in (comment.get('body') or '')
               and r.same_user(comment.get('user', {}).get('login', ''), r.CONFIG['user'])
               for comment in comments):
        if not r.same_user(r.api('user')['login'], r.CONFIG['user']):
            raise ValueError('GitHub account changed before reply')
        r.api(f'repos/{repo}/issues/{number}/comments', payload={'body': reply_body(item, record)})
    record['status'] = 'done'
    save(state)
    LOG.info('Completed %s', item['key'])


def fix_one(repo, number, pr, item, state, dry_run=False):
    record = state.get(item['key'], {})
    if record.get('status') == 'pending_reply':
        if not dry_run:
            finish_reply(repo, number, item, record, state)
        return
    comments = r.paginated(f'repos/{repo}/issues/{number}/comments?per_page=100')
    if any(marker(item) in (comment.get('body') or '')
           and r.same_user(comment.get('user', {}).get('login', ''), r.CONFIG['user']) for comment in comments):
        if not dry_run:
            state[item['key']] = {'status': 'done'}
            save(state)
        return
    with tempfile.TemporaryDirectory(prefix='fix-', dir=ROOT / 'autofix-work') as directory:
        work = Path(directory)
        context = prepare(work, repo, number, pr, item)
        before = files_snapshot(work / 'head')
        result = edit(work, item)
        changed = changed_paths(before, files_snapshot(work / 'head'), context)
        (ROOT / 'last-autofix.json').write_text(json.dumps({'request': item, 'result': result, 'files': changed}, indent=2))
        if result['status'] == 'not_actionable':
            if changed or result.get('commits') != []:
                raise ValueError('Non-actionable feedback must not change source or propose commits')
            if not dry_run:
                state[item['key']] = {'status': 'ignored', 'summary': result['summary'], 'evidence': result['evidence']}
                save(state)
            LOG.info('No actionable change for %s: %s', item['key'], result['summary'])
            return
        if result['status'] == 'blocked':
            raise ValueError('Model could not safely fix request: ' + result['limitations'])
        if (result['status'] == 'fixed') != bool(changed):
            raise ValueError('Fix report does not match the actual changes')
        plan = commit_plan(result, changed)
        context['files'] = [{'filename': name} for name in changed] if changed else context['files']
        validation = run_checks(work, repo, context, r.CONFIG['validation'],
                                api=r.api, gh=r.gh, extract_snapshot=r.extract_snapshot, log=LOG)
        (ROOT / 'last-autofix-validation.json').write_text(json.dumps(validation, indent=2))
        docs_only = all(name.startswith('docs/') or Path(name).suffix.lower() in ('.md', '.mdx', '.rst')
                        for name in (changed or [entry['filename'] for entry in context['files']]))
        if (validation['status'] != 'passed' and
                not (validation['status'] == 'not_applicable' and docs_only)):
            raise ValueError('Fix not published: ' + validation_summary(validation))
        record = {**result, 'validation': validation_summary(validation), 'status': 'pending_reply'}
        reply_body(item, record)
        if dry_run:
            LOG.info('Dry run %s: %s; %s; no push or reply', item['key'], result['summary'], record['validation'])
            return
        ensure_fresh(repo, number, pr, item)
        if changed:
            git(work, 'init', '--bare', str(work / 'controller-git'))
            url = f'https://github.com/{repo}.git'
            git(work, 'fetch', '--no-tags', '--depth=1', url, f'refs/pull/{number}/head', network=True)
            fetched = git(work, 'rev-parse', 'FETCH_HEAD').decode().strip()
            if fetched != pr['head']['sha']:
                raise ValueError('Fetched head changed during fix')
            git(work, 'update-ref', 'HEAD', fetched)
            git(work, 'read-tree', fetched)
            git(work, 'add', '--', *changed)
            patch = git(work, 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--binary')
            if not patch or len(patch) > 250000:
                raise ValueError('Empty fix or fix patch exceeds 250 KB')
            git(work, 'read-tree', fetched)
            record['commit_shas'] = []
            for entry in plan:
                git(work, 'add', '--', *entry['files'])
                git(work, '-c', 'user.name=' + r.CONFIG['user'],
                    '-c', 'user.email=' + r.CONFIG['autofix']['commit_email'],
                    'commit', '-m', entry['message'], '-m', 'Refs: ' + item['url'])
                record['commit_shas'].append(git(work, 'rev-parse', 'HEAD').decode().strip())
            record['commit'] = record['commit_shas'][-1]
            record['branch'] = pr['head']['ref']
            record['original_head'] = pr['head']['sha']
            ensure_fresh(repo, number, pr, item)
            state[item['key']] = record
            save(state)
            # Push a child of the captured head only if that exact remote head is still current.
            git(work, 'push', '--force-with-lease=refs/heads/' + pr['head']['ref'] + ':' + fetched,
                url, 'HEAD:refs/heads/' + pr['head']['ref'], network=True)
        state[item['key']] = record
        save(state)
        finish_reply(repo, number, item, record, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Discover requests; no model, push, or reply')
    parser.add_argument('--dry-run', action='store_true', help='Generate and validate one fix; no push, reply, or state changes')
    parser.add_argument('--pr', help='Only owner/repo#number in the configured allowlist')
    args = parser.parse_args()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    r.CONFIG = r.load_config(ROOT)
    settings = r.CONFIG.get('autofix', {})
    if not settings.get('enabled') and not args.check and not args.dry_run:
        raise ValueError('Auto-fix is disabled in config.json')
    if not r.CONFIG.get('repositories'):
        raise ValueError('Explicit repository allowlist required')
    LOG.setLevel(logging.INFO)
    handler = RotatingFileHandler(ROOT / 'autofixer.log', maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.addHandler(logging.StreamHandler())
    with (ROOT / 'autofix.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('Another auto-fix poll is still running')
            return
        if not (args.check or args.dry_run) and not r.claim_poll_window(
                path=ROOT / 'autofix-poll-schedule.json', log=LOG):
            return
        if not r.same_user(r.api('user')['login'], r.CONFIG['user']):
            raise ValueError('Authenticated GitHub user does not match configured author')
        search = ['search', 'prs', '--author', r.CONFIG['user'], '--state', 'open', '--limit', '1000',
                  '--json', 'number,repository']
        for repo in r.CONFIG['repositories']:
            search += ['--repo', repo]
        found = json.loads(r.gh(*search))
        if len(found) >= 1000:
            raise ValueError('Author search hit the 1000 PR cap; narrow the repository allowlist')
        path = ROOT / 'autofix-state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        (ROOT / 'autofix-work').mkdir(exist_ok=True)
        attempted = 0
        for candidate in found:
            repo, number = candidate['repository']['nameWithOwner'], candidate['number']
            if args.pr and args.pr != f'{repo}#{number}':
                continue
            pr = r.api(f'repos/{repo}/pulls/{number}')
            if not own_pr(repo, pr):
                continue
            for item in requests(repo, number):
                record = state.get(item['key'], {})
                if record.get('status') in ('done', 'ignored'):
                    continue
                LOG.info('Request %s %s', item['key'], item['url'])
                if args.check:
                    continue
                if (record.get('status') != 'pending_reply' and
                        time.time() - record.get('last_attempt', 0) < settings.get('retry_seconds', 3600)):
                    continue
                if attempted >= settings.get('max_requests_per_poll', 1):
                    return
                attempted += 1
                if not args.dry_run:
                    state.setdefault(item['key'], {})['last_attempt'] = time.time()
                    save(state)
                try:
                    fix_one(repo, number, pr, item, state, args.dry_run)
                except Exception:
                    LOG.exception('Auto-fix deferred %s; no success reply posted', item['key'])
                # Refresh before processing another request after a push.
                pr = r.api(f'repos/{repo}/pulls/{number}')
        LOG.info('Poll complete: %s PRs, %s fix attempts', len(found), attempted)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        LOG.exception('Auto-fix poll failed')
        raise SystemExit(1)
