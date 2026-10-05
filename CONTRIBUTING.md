# Contributing to Hermes Console

Thanks for your interest! This project aims to stay small, readable and safe. A few ground rules keep it that way.

## What this project is

A single-file Flask app (`app.py`) + Jinja templates that manages one Hermes Agent instance pointed at by `HERMES_HOME`. Design goals:

- **Generic** — no hard-coded paths, IPs, credentials or instance-specific values. Everything configurable goes through env vars.
- **Safe by default** — never render secrets back to the UI; validate uploads; back up before destructive actions.
- **Readable** — plain Flask, minimal dependencies, no build step for the frontend.

## Ground rules

1. **No secrets in code or docs.** Never commit API keys, passwords, tokens, real server IPs or personal paths. `.gitignore` covers runtime data (`console.db`, `.env`, backups). If you add config, use an env var with a safe default.
2. **No new heavy dependencies** without opening an issue first. Current deps: flask, requests, ruamel.yaml, gunicorn.
3. **Keep the frontend dependency-free** (vanilla JS + inline CSS in templates). No npm/bundler.
4. **Destructive operations must be reversible or confirmed** — restore already auto-snapshots and requires typing `RESTORE`.
5. **Test your change** — run `python app.py`, click through the affected page. There's no CI yet; manual smoke is expected.

## How to contribute

### Report a bug / request a feature
Open an **Issue**. Include: what you did, what happened, what you expected, and your `HERMES_HOME` layout if relevant. Redact any secrets from logs.

### Submit a change (Pull Request)
1. Fork the repo, create a branch off `main` (`git checkout -b fix/your-thing`).
2. Make focused changes — one logical change per PR.
3. Update `README.md` if you change behavior, env vars or the feature list.
4. Open a PR against `main` with a short description of *what* and *why*.

A maintainer reviews and merges. Small, well-scoped PRs get merged fastest.

## Adding a model provider

Providers live in `app.py`. Two places:

- `ENDPOINT_FIXES` — override a base URL from the framework registry when the official endpoint changes.
- `BUILTIN_PROVIDERS` — add a provider the framework registry doesn't have. Each entry:
  ```python
  'provider-id': {
      'name': 'Display Name',
      'cn_name': '中文显示名',
      'group': 'sub' | 'api' | 'local',   # subscription / pay-as-you-go / local
      'env_vars': ['SOME_API_KEY'],        # first is the primary key env var
      'base_url_env': 'SOME_BASE_URL',     # optional env var for the base URL
      'base_url': 'https://official-endpoint/v1',
  },
  ```
  then add a row to `CN_PROVIDERS`.

**Verify endpoints against the provider's official docs before adding.** Subscription (Coding Plan) and pay-as-you-go endpoints are frequently different — using the wrong one can silently bypass plan quota or trigger pay-as-you-go billing. Add a code comment with the doc source and the date you verified it.

## Code style

- Python 3.10+, standard formatting, type hints optional.
- Keep route handlers thin; push logic into helper functions.
- Multi-worker safety: shared state goes through the file-backed helpers (`_ustate_read` / `_ustate_write`), not module-level dicts.

## License

By contributing, you agree your contributions will be licensed under the MIT License.
