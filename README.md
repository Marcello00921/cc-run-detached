# cc-run-detached

Run a long command **fully detached** from an AI coding agent's shell, with its state in a file,
so the agent can check on it with short calls instead of a long blocking wait.

## The problem

Claude Code runs background shell commands (`run_in_background`) under a time limit: 30 minutes
by default, 2 hours at most. A command started by a foreground subagent also stops when that
subagent finishes. Renders, long test series and builds outlive those limits and are cut off
mid-way, **with no exit code**. A plain `nohup` / `start` is not enough on Windows, where a tree
kill (`taskkill /T`, which is what tools like `tree-kill` do) still reaches a direct child.

Long blocking waits have a second cost. While the agent sits in one long tool call, its prompt
cache can expire, and the next request pays for a full cache rewrite.

## How it works

- **Double launch:** `--launch` starts a short-lived starter, which starts the watcher
  detached and exits at once. The watcher is orphaned, so a tree kill of the launcher does
  not reach it.
- **Windows:** the watcher has no console (`DETACHED_PROCESS` plus `CREATE_NEW_PROCESS_GROUP`)
  and tries to leave the parent's job object (`CREATE_BREAKAWAY_FROM_JOB`, falling back if the
  job forbids it). The command runs with `CREATE_NO_WINDOW`, so no console window pops up.
- **macOS / Linux:** the watcher starts in a new session (`setsid`).
- The watcher runs the command, sends stdout and stderr to `log.txt`, and writes
  `status.json`: `launching -> running -> finished | failed`, with the exit code. If the watcher
  is killed or the machine reboots, the next `--status` reports `failed` and gives the reason.

## Install

Python 3.8+ and the standard library only. Copy `run_detached.py` anywhere, for example into
your project's `tools/` folder.

## Usage

```sh
python run_detached.py --launch build -- npm run build      # returns at once
python run_detached.py --status build                       # state, rc, last 5 log lines
python run_detached.py --wait build --timeout 270           # blocks at most 270 s
python run_detached.py --list
python run_detached.py --launch e2e --cwd ./app -- pytest -x
```

| exit code of `--status` / `--wait` | meaning |
|---|---|
| 0 | finished, command exit code 0 |
| 3 | failed (command exit code != 0, or the watcher vanished) |
| 4 | still running (the `--timeout` passed) |
| 2 | unknown name or usage error |

The `--timeout` is hard. The loop reads only `status.json` and checks the watcher process at most
every 30 s, so an agent waiting in slices of about 270 s stays inside a 5-minute prompt-cache
window.

State goes to `$RUN_DETACHED_HOME`, or by default to `%LOCALAPPDATA%\run-detached` on Windows
and `$XDG_CACHE_HOME/run-detached` (`~/.cache/run-detached`) elsewhere.

## Using it with Claude Code

Allow the tool in `.claude/settings.json`, so the agent does not stop for a permission prompt
on every status check:

```json
{
  "permissions": {
    "allow": [
      "Bash(python tools/run_detached.py:*)"
    ]
  }
}
```

Then tell the agent in `CLAUDE.md` how to use it:

```markdown
## Long commands
Anything that may take more than a few minutes (builds, renders, full test suites):
- start it with `python tools/run_detached.py --launch <name> -- <command>`;
- check it with `--wait <name> --timeout 270`, repeated, instead of one long blocking call;
- read `--status <name>` for the exit code and the tail of the log.
```

## Platform notes

- Tested on Windows 11 (Python 3.13). The POSIX path (`start_new_session`, `os.kill(pid, 0)`)
  is standard library and covered by the same tests, but it has not been run on macOS or Linux
  yet. Reports are welcome.
- On Windows, the liveness check uses `tasklist` and requires the watcher to be a `python`
  process, because PIDs get reused. Under heavy load a failing `tasklist` counts as "alive",
  so a dead watcher can take up to about 30 s longer to be reported.
- If a job object forbids breakaway, the watcher still starts. It survives a tree kill, but not
  the closing of that job.

## Tests

```sh
python -m unittest -v
```

The suite includes a survival test: a launcher process starts a job, its whole process tree is
killed, and the job must still finish with rc 0. It also has a negative control:
with `RUN_DETACHED_NO_DETACH=1` the command runs as a plain child, and the same kill must take
it down. That proves the survival test can fail.

## License

MIT, see [LICENSE](LICENSE).
