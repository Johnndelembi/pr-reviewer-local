# 0.2.0 — Account-based provider setup

Adds `--provider codex` using bundled official Codex CLI 0.114.0. Users can sign into their own ChatGPT account with a browser device code, without an AI API key. The `auth` Compose service provides `login`, `auth-status`, and `logout`; a private named volume preserves sessions and refreshed credentials across restarts and upgrades. GitHub access remains separately configured.

Adds `--provider vertex` for Google Cloud Vertex AI through Application Default Credentials (ADC), alongside the existing `--provider gemini` Gemini Developer API key mode. Vertex uses the project's Google Cloud IAM, billing and quota; the private ADC file is mounted from `data/`. It does not reuse Gemini CLI OAuth. Anthropic API-key setup remains available with `--provider anthropic`.

Codex uses the existing bounded file tools through a local stdio MCP server. Shell, browser, apps, hooks, image tools and subagents are disabled; untrusted repository configuration is never loaded. GitHub, API-provider and Notify credentials are excluded from the CLI environment. Review results are schema-validated and subprocess failures cannot become clean approvals. Auto-fix writes remain restricted to `head/`.

Existing API providers and configurations remain supported. Posting and auto-fix still require explicit opt-in. The README includes provider setup, credential handling and upgrade steps. Official Linux binaries are pinned and verified against package SHA-512 integrity values during the multi-platform Docker build.

The running desktop reviewer is unaffected. Docker test builds exercise the provider configuration and Codex runtime without making live GitHub or AI model requests. Run `reviewer doctor` and a dry-run review after configuring an account to verify its live access.
