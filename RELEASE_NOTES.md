# 0.1.2 — portable Docker distribution

The reviewer can now run independently of the original Mac installation. Repositories, skills, AI provider/model, schedule, signatures and optional integrations are configured per installation. Configuration and review state live outside the image.

The image uses a new API-backed agent with bounded file tools rather than bundling the personal Codex login. It supports OpenAI, native Anthropic Messages, Gemini's OpenAI-compatible endpoint, OpenRouter, tool-capable Ollama models and compatible Chat Completions gateways. Operator-selected skills are embedded in the prompt. The model has no shell, credential access or arbitrary network tool; auto-fix gets writes only below the captured head source.

The existing GitHub allowlist, revision freshness checks, consolidated finding policy, companion source, finding-resolution notices, trusted ordinary-comment handling and guarded auto-fix controller are retained. Default installation only discovers PRs; posting and fixing require explicit opt-in. Build/test execution is an optional dedicated-runner configuration, with archive streaming that works when the controller is containerized.

Installation provides a network-free setup service, configuration doctor, preview commands, persistent state and Compose controls. Publication provides a multi-platform Docker Hub release workflow and manual registry commands. The default skill is original generic guidance; personal or paid plugin skills and credentials are excluded. Source is distributed under the included MIT license.

Verified: 283 tests in Linux ARM64, synthetic HTTP review/fix tool roundtrips, real disposable Docker validation stages with cleanup, fresh setup/doctor, state persistence across containers, Compose parsing and non-root/private-state exclusion checks. See release-checks.json for the local image ID.

Debian package downloads use HTTPS with bounded retries during source builds.

Publication targets `docker.io/johnndelembi/pr-reviewer-local`. This release updates the install files from the initial unpublished GHCR target. Publication is performed locally because GitHub Actions was blocked by an account billing lock. The workflow supports future Docker Hub releases after account access and `DOCKERHUB_TOKEN` are configured.

Live GitHub PR reviews/fixes and paid-provider account calls were not performed. `release-checks.json` records the original local verification snapshot; the GitHub release records subsequent platform and publication results. The running desktop reviewer remains separate from this distribution.
