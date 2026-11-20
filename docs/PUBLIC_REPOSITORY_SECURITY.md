# Public repository security boundary

This repository is intentionally suitable for public source-code visibility. Keep private operational material outside it.

Never publish:
- `.env` files or any populated credential file;
- OKX API keys, secret keys, passphrases, recovery codes, or access tokens;
- account balances, account identifiers, private trade/fill history, or personal information;
- local databases, runtime state, logs, private exports, or unreviewed datasets;
- GitHub personal access tokens or other CI credentials.

The repository's CI workflow has `contents: read` permission, uses standard GitHub-hosted Ubuntu runners, installs dependencies, runs tests and compile checks, and has no secrets or exchange integration. Do not add credentials or order-submission steps to CI.

The public source code and tests may be copied, modified, or redistributed by others. Public visibility does not transfer ownership of the user's exchange account or grant permission to trade.
