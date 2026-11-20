# NEBULA v0.16.5 — result auto-copy

## Change

- Every successful CLI command result is printed to stdout and copied to the system clipboard when a supported clipboard utility is available.
- The copied text is exactly the result text; source code and command input are never copied.
- Linux: `wl-copy`, `xclip`, or `xsel`; macOS: `pbcopy`; Windows: `clip.exe`.
- Clipboard support is best-effort. Missing utilities, unavailable sessions, timeouts, or clipboard errors do not fail the NEBULA command.
- Set `NEBULA_AUTO_COPY_RESULT=false` to disable the default.
- Help and error output are excluded.

## Safety

No exchange endpoint, API credential, order gate, strategy, or trading behavior changed.
