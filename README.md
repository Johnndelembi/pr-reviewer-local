# PR Reviewer

Run a configurable GitHub PR reviewer in Docker. Choose repositories, trusted skill files, an AI provider, model and polling schedule. Review state survives restarts. Optional auto-fix handles actionable regular comments, inline comments and submitted reviews on your own PRs.

The default install only discovers PRs. Review publication and auto-fix require explicit configuration. No personal credentials, repository allowlist, logs, review history, paid plugin skills or source snapshots are included in the image.

## Quick start

Requires Docker Engine/Desktop with Docker Compose. Images are designed for Linux AMD64 and ARM64, including Docker Desktop on macOS and Windows. Use a shell with the commands below; on Windows use WSL or adapt paths for PowerShell.

Download the installation files from the public release:

```sh
mkdir pr-reviewer-local
cd pr-reviewer-local
curl -fsSLO https://raw.githubusercontent.com/Johnndelembi/pr-reviewer-local/v0.2.0/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/Johnndelembi/pr-reviewer-local/v0.2.0/.env.example
cp .env.example .env
mkdir -p config data
docker compose pull
```

The image is `johnndelembi/pr-reviewer-local:0.2.0`. Source code is not needed to run it. For a local source build, run `docker build -t pr-reviewer:local .` in the source directory and set `REVIEWER_IMAGE=pr-reviewer:local` in `.env` before using Compose.

Initialize your configuration (substitute your GitHub user and repositories):

```sh
docker compose run --rm setup init \
  --user YOUR_GITHUB_USERNAME \
  --repo your-org/your-repo \
  --repo your-org/another-repo \
  --provider codex \
  --model gpt-6.1-sol
```

The one-off `setup` service has a writable `/config` mount and no network. The running reviewer mounts the same configuration read-only. `init` refuses to overwrite an existing configuration. It creates `config/config.json` and `config/skills/review.md`.

On Linux, if your user is not UID/GID 1000, set `REVIEWER_UID` and `REVIEWER_GID` in `.env` to the results of `id -u` and `id -g` before initialization. Keep the bind-mounted `config/` and `data/` directories owned by that user. macOS Docker Desktop usually maps bind-mount permissions automatically.

Choose how to connect your AI account:

**ChatGPT browser sign-in (no AI API key):** the `--provider codex` example above uses the bundled Codex CLI. Run:

```sh
docker compose run --rm auth login
```

The terminal prints a URL and a one-time code. Open that URL in your own browser, sign in to your ChatGPT account, and enter the code. If needed, enable device-code login in your ChatGPT security settings or workspace permissions. No browser runs inside the container and no callback port is exposed. Account/workspace access, model availability and Codex usage limits apply; unattended API-key automation remains OpenAI's recommended default. See [official Codex authentication guidance](https://learn.chatgpt.com/docs/auth#login-on-headless-devices).

Check or clear this installation's login:

```sh
docker compose run --rm auth auth-status
docker compose run --rm auth logout
```

The private `codex-login` Docker volume persists the session and refreshed tokens across container restarts and image upgrades. Never share it or add it to an image. `docker compose down` preserves it; `docker compose down -v` deletes it and requires signing in again. Stop the reviewer before logout or account changes. Changing `REVIEWER_UID` changes the session directory and requires another login. The auth service receives no GitHub or provider keys and needs no existing reviewer configuration.

If a review reports an authentication failure, stop the reviewer, run `docker compose run --rm auth auth-status`, then sign in again with `docker compose run --rm auth login`. If Codex is still holding a stale session, run `docker compose run --rm auth logout` first. Confirm the session with `docker compose run --rm reviewer doctor`, then restart the reviewer.

**AI API key:** initialize with `--provider openai` (or another supported API provider) instead, then put that provider's key in `.env` as `AI_API_KEY`. Browser login is unnecessary for this mode. For existing configurations, edit `ai.provider` and `model` instead of rerunning `init`.

**Gemini API key:** choose `--provider gemini` and put a Gemini Developer API key in `AI_API_KEY`. This connects to Google's Gemini API directly.

**Google account through Vertex AI:** for Google account authentication, use Google Cloud Application Default Credentials (ADC). Enable the Vertex AI API on your Google Cloud project, sign in on a trusted machine, then copy the ADC file into the private `data/` directory:

```sh
gcloud config set project YOUR_GOOGLE_CLOUD_PROJECT
gcloud services enable aiplatform.googleapis.com
gcloud auth application-default login
install -m 600 "$HOME/.config/gcloud/application_default_credentials.json" data/google-application-default-credentials.json
```

Initialize with `--provider vertex --project YOUR_GOOGLE_CLOUD_PROJECT --location global --model google/gemini-3.5-flash`. Pick a model currently available to your project and location. In headless setups, `gcloud auth application-default login --no-launch-browser` prints a URL and prompts for the code from a trusted browser. The ADC file contains refresh credentials, so protect it like a password and never commit or share it. `data/` is private and git-ignored. Vertex requests use Google Cloud billing, IAM and quota for the selected project. See [Google's ADC login guide](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/login) and [Vertex AI's OpenAI-compatible API guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/openai).

Every provider mode needs separate GitHub access. Open `.env` and configure:

```dotenv
GH_TOKEN=your-scoped-github-token
# Only needed for an API provider; leave blank for Codex browser sign-in.
AI_API_KEY=
REVIEWER_IMAGE=johnndelembi/pr-reviewer-local:0.2.0
```

Keep `.env` private and out of Git (`chmod 600 .env` on Unix). The GitHub token must belong to the configured `user`. Use a fine-grained personal access token limited to the repositories in the allowlist, with metadata/contents read access, pull requests read/write for reviews, and issue-comment read access. Auto-fix additionally needs contents write and issue-comment write. Organization policies may require token approval. GitHub App installation tokens and enterprise hosts are not supported in this version.

Check configuration and GitHub access before running AI:

```sh
docker compose run --rm reviewer doctor
docker compose run --rm reviewer check
```

`doctor` checks configuration and credential presence, Codex login status, and refreshes Vertex ADC without sending a model request. It does not verify model access or API-key permissions. `check` discovers PRs without AI calls or GitHub writes. Eligible PRs must be open, non-draft, authored by someone else, and directly request review from the configured user. Team-only requests are not discovered. Once reviewed, PRs are watched for new commits until they close.

Preview a discovered PR (uses your selected provider, but does not post):

```sh
docker compose run --rm reviewer review --dry-run --pr your-org/your-repo#123
```

Read `data/last-review.json`, `data/last-validation.json` and the logs. To enable posting, set `"publish_reviews": true` in `config/config.json`, then start:

```sh
docker compose up -d --no-build
docker compose logs -f --tail=100 reviewer
```

With `publish_reviews=false`, the service performs discovery only. A manual `review` command defaults to a preview. `review --limit N` bounds a one-off batch. `docker compose down` stops the service while retaining your bind-mounted state. Run a single service instance per `data/` directory.

## Providers and models

Choose one provider during setup. The main account options are ChatGPT through Codex sign-in, Google Gemini through either an API key or Google Cloud Vertex AI ADC, and Anthropic through an API key. Codex CLI 0.114.0 is pinned for its verified local MCP transport and bundled for Linux AMD64 and ARM64 from integrity-checked official packages; no host CLI installation is required. Browser mode uses your own ChatGPT account through Codex, with the restricted file tools exposed through a local stdio MCP server. The original desktop runner remains separate.

Set `ai.provider` and top-level `model` in `config/config.json`. API providers use `AI_API_KEY` with the generated configuration; `ai.api_key_env` can name another variable for direct Docker deployments. Select a model that supports function/tool calling and can handle your source context.

| `ai.provider` | Default endpoint | Authentication / model |
| --- | --- | --- |
| `codex` | Bundled Codex CLI / ChatGPT | Browser device-code login; no AI API key |
| `openai` | `https://api.openai.com/v1` | OpenAI API key, exact OpenAI model ID |
| `anthropic` | `https://api.anthropic.com/v1` | Anthropic API key, exact Claude model ID; native Messages API |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | Gemini Developer API key, exact Gemini model ID |
| `vertex` | Google Cloud Vertex AI OpenAI-compatible endpoint | Google Cloud ADC user login, project ID and location; model ID such as `google/gemini-3.5-flash` |
| `openrouter` | `https://openrouter.ai/api/v1` | OpenRouter API key and provider/model ID |
| `ollama` | `http://host.docker.internal:11434/v1` | Set `ai.api_key_env` to `""`; choose an installed tool-capable model |
| `compatible` | Set `ai.base_url` explicitly | OpenAI Chat Completions-compatible gateway with tool calling |

For Vertex AI, set `ai.provider` to `"vertex"`, add `ai.project_id` and `ai.location`, and remove `ai.api_key_env` and `ai.base_url`. For ChatGPT browser mode, set `ai.provider` to `"codex"`, remove `ai.api_key_env` and `ai.base_url`, choose a Codex model available to your account, and run the login command. Existing repository and skill settings remain in place.

Codex browser mode uses a conservative 64,000-token text-only model profile and disables built-in patch/image access. Select an exact model ID available to your account; the model ID is never substituted.

The compatibility providers use `/chat/completions`; this is distinct from the original Codex CLI's Responses API provider configuration. Anthropic uses `/messages`. Model availability and feature support are provider-specific. There is no silent model fallback: provider errors, malformed output and budget exhaustion cannot become automatic approvals.

For example, change provider without changing installed repositories or skills:

```json
{
  "model": "YOUR_CLAUDE_MODEL_ID",
  "reasoning": null,
  "ai": {
    "provider": "anthropic",
    "api_key_env": "AI_API_KEY",
    "max_turns": 80,
    "max_tokens": 16000,
    "timeout_seconds": 120,
    "job_timeout_seconds": 1800
  }
}
```

Merge that fragment into the complete configuration; do not replace the whole file with it. Set `.env`'s `AI_API_KEY` to the chosen provider's key and recreate the service after changing environment variables: `docker compose up -d --force-recreate --no-build`.

For Ollama, make its server reachable from Docker and set:

```json
"model": "YOUR_INSTALLED_TOOL_CAPABLE_MODEL",
"reasoning": null,
"ai": {"provider": "ollama", "api_key_env": ""}
```

`host.docker.internal` is mapped to the Docker host in Compose. Local servers must listen on an interface reachable from containers; Docker Desktop/host firewall configuration varies. For a remote private HTTP gateway, explicitly set `ai.allow_insecure_http=true`; use HTTPS for remote credentials. URLs with embedded credentials, query strings or fragments are rejected. Provider redirects are not followed.

`reasoning` is sent only to OpenAI; use a value supported by your selected model, or `null` to omit it. `ai.max_turns` (maximum 200), `ai.max_tokens` (maximum 32,000), request timeouts and the total job timeout bound API attempts. Codex mode maps `ai.max_turns` to a maximum file-tool call count and uses `ai.job_timeout_seconds` for the total deadline; API request/token settings do not apply to the CLI backend. Large PRs can exhaust context or budgets and will be deferred rather than approved.

### Docker secrets

For direct Docker deployments, credentials can be mounted as files and selected with `GH_TOKEN_FILE` and `AI_API_KEY_FILE` instead of environment values. Remove the corresponding plain environment value when using a secret. The model never gets credential tools. Example:

```sh
docker run --rm --read-only --tmpfs /tmp:mode=1777 \
  --mount "type=bind,src=$(pwd)/config,dst=/config,readonly" \
  --mount "type=bind,src=$(pwd)/data,dst=/data" \
  --mount "type=bind,src=$(pwd)/secrets/github-token,dst=/run/secrets/gh,readonly" \
  --mount "type=bind,src=$(pwd)/secrets/ai-key,dst=/run/secrets/ai,readonly" \
  -e GH_TOKEN_FILE=/run/secrets/gh -e AI_API_KEY_FILE=/run/secrets/ai \
  pr-reviewer:local doctor
```

Secret files must be readable by the container's configured UID. Do not add real secrets to this source directory or commit them.

## Repositories, skills and schedule

`repositories` is an explicit allowlist checked at discovery, review and posting. `context_repositories` permits additional explicitly cited companion source; it does not permit reviewing or modifying those repositories. Pinned Git submodule context, companion PR heads, finding-resolution follow-ups and the existing two-round finding policy are preserved.

Edit `config/skills/review.md`, add Markdown files below `config/skills/`, and list their container paths in `skill_files`. List the repair skills separately in `autofix.skill_files`. Changes apply on the next invocation. Include any required reference files explicitly in the list; API agents cannot read files outside downloaded source workspaces. This image includes an original generic review skill, not your private installed plugins. Only redistribute third-party skills when their licenses permit it.

Review signatures use `display_name`. Each install's identity and configuration stay outside the image.

Default scheduling is every 10 minutes in UTC. To reproduce your current reviewer schedule:

```json
"schedule": {
  "timezone": "Africa/Dar_es_Salaam",
  "interval_minutes": 10,
  "daytime_interval_minutes": 30,
  "daytime_start_hour": 11,
  "daytime_end_hour": 17
}
```

An optional `daytime_review` object selects a different `model` and `reasoning` from 11:00 to 17:00 in the configured timezone. The top-level provider is shared. Saved poll windows prevent duplicate batches after restarts. Missed windows are not replayed. A long review can delay the next poll. Configuration edits are read before the next batch; restart the service for immediate application.

## Optional build/test validation

Default installs perform static review and state clearly that builds/tests were not run. To execute Node/Python checks, use a dedicated Docker runner host, build trusted runner images, and opt in:

```sh
make runner-images
# Edit config/config.json: set validation.enabled to true.
# On Linux, set DOCKER_GID to the group owning /var/run/docker.sock if necessary.
docker compose -f compose.yaml -f compose.validation.yaml up -d --no-build
```

The controller needs Docker-daemon access to launch test jobs. Docker socket access is effectively host-admin access for the controller and is absent from the default Compose file. Use a dedicated runner host/VM; it is not a multi-tenant sandbox. Model tools cannot read that socket or execute commands. Test containers receive no reviewer credentials or Docker socket. They run as UID 1000, drop capabilities, use a read-only root, bounded CPU/memory/processes and a disposable memory-backed workspace. Dependency installation sees sanitized manifests; source checks have networking disabled.

`validation.input_transport="archive"` streams input through Docker into the disposable volume; it works when the controller itself runs in a container. It avoids interpreting container paths as host bind paths. Set runner image names or image IDs explicitly and build/pull only images you trust. Images are never selected by PR text. The default trusted runners support Node/pnpm and Python projects. Unknown build systems and integration suites needing outside services may remain blocked.

Optional `validation.workspace_dependencies` rules may supply explicitly approved build-only dependencies; they must identify repositories/revisions belonging to your installation. No Notify-specific dependency configuration is distributed.

Failed, blocked or timed-out validation prevents clean approval. Disabled validation is reported as static review, not as passed tests. The image preserves per-target logs, dependency provenance and finding-resolution notices even when an overall review remains deferred.

## Optional auto-fix

Auto-fix remains disabled until `autofix.enabled=true`. Supply a correct `autofix.commit_email`, trusted `autofix.skill_files`, and working build/test validation. The fixer handles trusted ordinary issue comments, inline comments and submitted review summaries on your own open PRs. Feedback must be authored by the configured user or an owner/member/collaborator. Praise, acknowledgments and irrelevant suggestions do not cause edits.

Preview without pushing:

```sh
docker compose run --rm reviewer autofix --dry-run --pr your-org/your-repo#123
```

The agent can edit only files under the captured head snapshot. The controller verifies changed files, the commit plan, validation and the unchanged remote head before pushing. It uses a lease against that captured commit; it never intentionally replaces someone else's newer commit. Commits and feedback replies are deduplicated in persistent state. Changes involving submodules or agent configuration require manual attention. Auto-fix is not enabled merely by enabling review publication.

Optional Notify SMS notifications are off by default. To enable them, configure `sms_notifications.enabled`, `enabled_at` (Unix timestamp), `recipient`, and `sender_id`; provide `NOTIFY_API_KEY` or `NOTIFY_API_KEY_FILE` plus optional `NOTIFY_BULK_SMS_API_BASE_URL` to the container explicitly. No recipient or sender is bundled. Docker mode sends concise deterministic alert facts rather than invoking the old Mac-specific Codex CLI for wording.

## Build, test and publish

```sh
make test
make build
```

The test image runs the existing reviewer/fixer policy suite plus provider protocol, output-schema, path-boundary, credential-redirect and portable configuration tests. Tests use synthetic source and mocked provider responses; they do not post to GitHub or call paid models.

To publish manually to Docker Hub:

```sh
docker login
make publish REGISTRY_IMAGE=johnndelembi/pr-reviewer-local VERSION=0.2.0
```

To use GHCR or another registry, log in to that registry and pass its image name through `REGISTRY_IMAGE`.

`make publish` builds and pushes Linux AMD64 and ARM64 manifests. Buildx/QEMU support must be available. The `Dockerfile` copies only explicit runtime files, includes the MIT license and runs as a non-root user. Build context and Git ignore rules exclude installation secrets and runtime state. Python dependencies are pinned, but base images and OS packages are not a fully reproducible lock; use release image digests when deploying a published build.

The included GitHub Actions workflow tests PRs and `main`, then publishes to Docker Hub when you push a version tag such as `v0.2.0`. For automated publication, create the repository secret `DOCKERHUB_TOKEN` with a Docker Hub access token limited to image pushes. Set repository variable `DOCKERHUB_USERNAME` to your Docker Hub username; optionally set `DOCKERHUB_IMAGE` to `namespace/repository`. It defaults to the lowercase GitHub owner/repository. GitHub Actions must be available on the account. Manual `make publish` runs locally and does not depend on Actions.

After release, distribute `compose.yaml`, `.env.example` and this guide. Consumers can pull the image, run `init`, configure their own account/provider/skills, preview and enable it. Back up `config/` and `data/`, and keep the private login volume if using Codex; upgrades replace the image, not those directories. Keep version tags or digests pinned when upgrading. External services and live provider/model capability still need verification in each installation.

## Architecture and boundaries

GitHub discovery, downloads, validation, state and posting remain in the deterministic controller. The agent gets only paginated file reads, literal searches, a final schema-validated result, and—only for auto-fix—bounded writes/deletions below `head/`. It has no shell, code execution, arbitrary HTTP, credentials or GitHub tools. Path traversal, symlinks and agent metadata are blocked. PR instructions cannot add tools or change the allowlist.

Downloaded source is sent to Codex or the AI provider you configure as needed for review. Choose a provider appropriate for your repository confidentiality requirements. A successful build or synthetic protocol test does not establish review quality for every model; use a real dry run before enabling posting.

License: MIT.

Google account login here uses official Google Cloud ADC credentials for Vertex AI. It does not reuse Gemini CLI OAuth. Google's [Gemini CLI policy](https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/faq.md) warns against using its OAuth credentials to access Gemini services from third-party software.

Implementation references: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Claude custom tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools), [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai), [Vertex AI OpenAI compatibility](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/openai), [Docker multi-platform builds](https://docs.docker.com/build/ci/github-actions/multi-platform/).

## Upgrade from 0.1.2

Stop the service, download the new Compose file, and set `REVIEWER_IMAGE=johnndelembi/pr-reviewer-local:0.2.0` in your existing `.env`. Keep `config/` and `data/`:

```sh
docker compose down
curl -fsSLO https://raw.githubusercontent.com/Johnndelembi/pr-reviewer-local/v0.2.0/compose.yaml
docker compose pull
```

API configurations keep working. For browser mode, edit `config/config.json` as described above, run `docker compose run --rm auth login`, then `docker compose run --rm reviewer doctor`. Preview a review before enabling publication. Start with `docker compose up -d --no-build`. Do not rerun `init` over an existing installation.
