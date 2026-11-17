# claude-review

Continuous development + independent code review on [nanocode](https://nanocode.xyz/).

- **Developer:** Claude Opus 5.5 (medium) on nanocode's Claude harness.
- **Reviewer:** Kimi K3.1 Pro Highspeed (max) on nanocode's Codex harness, via Kimi's own API.

One Python stdlib script, `review_loop.py`.

## How it works

1. Claude works on the task in turns (`nc exec`, one resumed session).
2. A background thread snapshots the working tree into `refs/review/*` (your branch, index and HEAD are never touched). Once 40+ lines changed, or any change after 10 minutes, it starts a review.
3. Kimi reviews only the incremental diff since the last reviewed snapshot, in a write-protected worktree, with no tools.
4. New medium/high findings are added to Claude's next turn. A findings ledger drops duplicates and delivers each finding once; turns and final rounds are capped.
5. When Claude ends with `TASK_DONE`, a fresh Kimi session reviews the whole change, with up to two fix rounds.

All state is in `.review/` in the target repo and nanocode's session journal, so rerunning the command after a crash resumes both sessions.

## Requirements

- `nanocode` (0.7.0+) with a Claude login (or an existing Claude Code login)
- Kimi CLI logged in (`kimi login`); its login at `~/.kimi-code/credentials/kimi-code.json` signs reviewer requests
- git, Python 3.9+

## Install

```bash
uv tool install git+https://github.com/maximebrugel/claude-review
# or: pipx install git+https://github.com/maximebrugel/claude-review
```

Or run the script directly: `./review_loop.py ...`

## Usage

```bash
claude-review -C path/to/repo                      # asks for the task, Enter to start
claude-review -C path/to/repo "task description"   # skip the prompt
claude-review --selftest
```

Like Claude Code, it prints a scrolling transcript in your terminal: each agent's reasoning, tool calls and replies, the findings Kimi reports and when they are sent to Claude, with a live status line at the bottom. Ctrl-C stops at any time; rerun the same command to resume. When output is piped, the same transcript is printed without colors or status line.

`.review/wt` is read-only on purpose; to start over, run `chmod -R u+w .review && rm -rf .review`.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `DEV_MODEL`, `DEV_THINK` | `opus`, `medium` | developer |
| `REV_MODEL`, `REV_THINK` | `k3.1-pro-highspeed`, `max` | reviewer (`k3` for Kimi K3) |
| `REV_BASE_URL` | `https://api.kimi.com/coding/v1` | reviewer API |
| `REV_API_KEY` | | use a plain API key instead of the Kimi CLI login |
| `REV_PROVIDER` | | OpenRouter endpoint pin, e.g. `moonshotai/highspeed` |
| `REVIEW_MIN_LINES` | `40` | lines changed before a review |
| `REVIEW_PERIOD` | `600` | seconds before any change triggers a review |
| `REVIEW_POLL` | `20` | seconds the tree must be still before a review |
| `REVIEW_MAX_TURNS` | `30` | developer turn cap |
| `REVIEW_FINAL_ROUNDS` | `2` | final review/fix rounds |

## Limits

- Kimi's API rejects nanocode's tools, so the reviewer works from the diff alone (whole changed functions included).
- Kimi logins last 15 minutes; the script refreshes before each review with a one-word `kimi -p` prompt.
- Findings reach Claude at its next turn, not mid-turn.
