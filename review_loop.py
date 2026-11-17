#!/usr/bin/env python3
"""Continuous dev + review loop on NanoCode.

  claude-review [-C path/to/repo]            # type the task, then watch both agents live; rerun resumes
  claude-review -C path/to/repo "task"       # same, task given up front (plain output when piped)

Developer (Claude) works in the repo via `nc exec`. A background thread snapshots the
working tree, and when enough has changed runs the reviewer (Kimi) on the incremental
diff inside a read-only worktree. New findings are fed to the developer at its next
turn. All state lives in .review/ and refs/review/*, so a rerun picks up where it left.
"""
import argparse, collections, hashlib, json, os, re, subprocess, sys, threading, time

E = os.environ.get
NC = E("REVIEW_NC", "nanocode")
DEV_MODEL, DEV_THINK = E("DEV_MODEL", "opus"), E("DEV_THINK", "medium")
REV_MODEL, REV_THINK = E("REV_MODEL", "k3.1-pro-highspeed"), E("REV_THINK", "max")
# The reviewer runs on nanocode's Codex harness pointed at the Kimi API, the one the Kimi CLI
# uses (a ChatGPT login refuses Kimi). Requests go through forwarder() below, which signs them
# with the Kimi CLI's own login. REV_API_KEY switches to a plain API key for another base URL
# (e.g. OpenRouter, where REV_PROVIDER can pin an endpoint such as moonshotai/highspeed).
REV_BASE_URL = E("REV_BASE_URL", "https://api.kimi.com/coding/v1")
REV_PROVIDER = E("REV_PROVIDER", "")
KIMI_CRED = os.path.expanduser(E("KIMI_CRED", "~/.kimi-code/credentials/kimi-code.json"))
REV_ENV = dict(os.environ)
MIN_LINES = int(E("REVIEW_MIN_LINES", 40))    # "meaningful change": review once this many lines changed
PERIOD = int(E("REVIEW_PERIOD", 600))         # "periodic": or any change, after this many seconds
POLL = int(E("REVIEW_POLL", 20))              # tree must also be unchanged for one poll (not mid-edit)
MAX_TURNS = int(E("REVIEW_MAX_TURNS", 30))    # hard cap on developer turns
FINAL_ROUNDS = int(E("REVIEW_FINAL_ROUNDS", 2))
MAX_DIFF = 150_000                            # max diff chars inlined in the reviewer prompt
SEND = ("high", "medium")                     # severities fed back to the developer; the rest is only logged

D, WT, LEDGER = ".review", ".review/wt", ".review/findings.json"
REVIEW_LOCK, LEDGER_LOCK, STOP = threading.Lock(), threading.Lock(), threading.Event()
PREV, LAST_T = None, time.time()
KIDS = set()
STATE = {"dev": ("idle", 0), "rev": ("idle", 0)}

# ---- transcript (inline, like Claude Code: scrolling output + one live status line) ------

TTY = sys.stdout.isatty()
OUT_LOCK, LAST_SPEAKER = threading.RLock(), [None]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
WHO = {"dev": ("Claude", "developer", "38;5;209"), "rev": ("Kimi", "reviewer", "38;5;75"),
       "final": ("Kimi", "final review", "38;5;75"), "loop": ("claude-review", "", "38;5;245")}
SEV_COLOR = {"high": "1;31", "medium": "33", "low": "2"}


def c(code, s):
    return "\x1b[%sm%s\x1b[0m" % (code, s) if TTY else s


def width():
    import shutil
    return shutil.get_terminal_size().columns


def status_line():
    spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
    parts = []
    for who in ("dev", "rev"):
        text, t = STATE[who]
        name = WHO[who][0]
        parts.append(c(WHO[who][2], (spin + " " if t else "  ") + name) + " " +
                     text + (c("2", " %d:%02d" % divmod(int(time.time() - t), 60)) if t else ""))
    led = load() if os.path.exists(LEDGER) else []
    n = collections.Counter(f["status"] for f in led)
    parts.append("findings %d sent · %d pending · %d low" % (n["sent"], n["new"], n["logged"]))
    parts.append(c("2", "ctrl-c to stop"))
    s = "  ·  ".join(parts)
    plain = ANSI.sub("", s)
    return s if len(plain) < width() else plain[:width() - 1]   # must stay on one line to be redrawn


def emit(lines, redraw=True):
    """Print transcript lines above the status line."""
    with OUT_LOCK:
        if TTY:
            sys.stdout.write("\r\x1b[K")
        for l in lines:
            sys.stdout.write(l + "\n")
        if TTY and redraw:
            sys.stdout.write(status_line() + "\r")
        sys.stdout.flush()


def show(who, kind, text):
    """kind: reply (full text), think (one dim line), tool, note."""
    with OUT_LOCK:   # speaker header + its lines stay together when both agents stream
        _show(who, kind, text)


def _show(who, kind, text):
    import textwrap
    name, role, color = WHO[who]
    lines = []
    if LAST_SPEAKER[0] != who:
        LAST_SPEAKER[0] = who
        lines += ["", c("1;" + color, "● " + name) + (c("2", " " + role) if role else "")]
    w = max(20, width() - 4)
    if kind == "think":
        t = " ".join(text.split())
        lines.append("  " + c("2;3", "✻ " + (t if len(t) <= w - 2 else t[:w - 3] + "…")))
    elif kind == "tool":
        lines.append("  " + c(color, "⏺ ") + text)
    else:
        for para in text.splitlines() or [""]:
            lines += ["  " + l for l in (textwrap.wrap(para, w) or [""])]
    emit(lines)


def say(msg):
    show("loop", "note", msg)


def status(who, text, busy=False):
    STATE[who] = (text, time.time() if busy else 0)

DEV_INTRO = """{task}

You are the developer. An independent reviewer inspects your changes in the background and its
findings arrive in later messages. When the whole task is complete and verified, end your reply
with a line containing only TASK_DONE."""

DEV_FINDINGS = """Independent code review findings on your changes. Fix each valid one; for an invalid
one say why in one line (it will not be raised again). Then continue the task, and end with
TASK_DONE once everything is complete.
"""

REV_PROMPT = """You are an independent code reviewer. You are READ-ONLY and have no tools: review from the diff
below, which shows whole changed functions. It is {scope}.
Report only real defects introduced by the diff: bugs, security holes, data loss, broken error
handling, serious performance problems{extra}. No style nits, no praise{wip}.

Already reported, do NOT repeat even if unfixed (the developer may have declined them):
{known}

Reply with one JSON object per line and nothing else:
{{"file": "path", "line": 12, "severity": "high|medium|low", "title": "short defect name", "detail": "why it is wrong and the fix, max 2 sentences"}}
If there is nothing to report reply exactly: NONE

{diff}"""


def git(*a, cwd=None, env=None):
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def ref(name):
    p = subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/review/" + name], capture_output=True, text=True)
    return p.stdout.strip() or None


def snapshot():
    """Commit the working tree to refs/review/snap through a private index: the developer's
    index, HEAD and branch are never touched."""
    env = dict(os.environ, GIT_INDEX_FILE=os.path.abspath(D + "/index"))
    git("add", "-A", env=env)
    tree, last = git("write-tree", env=env), ref("snap")
    if last and git("rev-parse", last + "^{tree}") == tree:
        return last
    # internal snapshot, never pushed: don't trigger a signing prompt every poll
    c = git("commit-tree", "--no-gpg-sign", tree, *(["-p", last] if last else []), "-m", "review snapshot")
    git("update-ref", "refs/review/snap", c)
    return c


def changed(a, b):
    return sum(sum(int(x) if x.isdigit() else 1 for x in l.split("\t")[:2])
               for l in git("diff", "--numstat", a, b).splitlines())


def nc(name, cwd, model, think, prompt, resume=True, extra=(), env=None):
    """One headless turn. The session id is saved even on failure, so a rerun resumes it
    (nc exec finishes interrupted turns first)."""
    path, mark = "%s/%s.session" % (D, name), "%s/%s.started" % (D, name)
    if resume and not os.path.exists(path) and os.path.exists(mark):
        # Killed before nc printed its id: find the journaled session by its first prompt.
        key = open(mark).read()
        out = subprocess.run([NC, "sessions"], cwd=cwd, env=env, capture_output=True, text=True).stdout
        hit = [l.split()[0] for l in out.splitlines() if key in l]
        if hit:
            open(path, "w").write(hit[0])
    if resume and not os.path.exists(mark):
        open(mark, "w").write(prompt.splitlines()[0][:60])
    sid =open(path).read().strip() if resume and os.path.exists(path) else None
    cmd = [NC, "exec", "-C", cwd, "-m", model, "-t", think, *extra] + (["-r", sid] if sid else []) + [prompt]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    KIDS.add(p)   # so ctrl-c can stop running turns
    out_l, err_l = [], []

    def pump(stream, acc, is_err):
        # nc exec streams the reply on stdout; reasoning paragraphs and "· Tool" lines on stderr.
        for line in stream:
            acc.append(line)
            line = line.rstrip("\n")
            if is_err:
                if line.startswith("· "):
                    show(name, "tool", line[2:])
                elif line.strip() and not line.startswith("session "):
                    show(name, "think", line)
            elif name == "dev":            # the reviewer's JSON reply is shown as parsed findings
                show(name, "reply", line)
    try:
        t = threading.Thread(target=pump, args=(p.stderr, err_l, True))
        t.start()
        pump(p.stdout, out_l, False)
        t.join()
        p.wait()
    finally:
        KIDS.discard(p)
    out, err = "".join(out_l), "".join(err_l)
    with open(D + "/nc.log", "a") as f:
        f.write("\n== %s %s\n%s" % (name, time.strftime("%F %T"), err))
    ids = re.findall(r"^session (\S+)$", err, re.M)
    if ids:
        open(path, "w").write(ids[-1])
    if p.returncode:
        raise RuntimeError("%s turn failed: %s" % (name, (err.strip().splitlines() or ["?"])[-1]))
    return out


TOKEN_LOCK = threading.Lock()


def rev_token():
    if E("REV_API_KEY"):
        return E("REV_API_KEY")
    with TOKEN_LOCK:
        d = json.load(open(KIMI_CRED))
        if d["expires_at"] - time.time() < 300:
            # Kimi logins last 15 minutes. Let the Kimi CLI refresh and rewrite its own login
            # instead of touching its refresh token ourselves. The CLI has no refresh command,
            # so this is a one-word headless prompt, run in an empty directory so its agent
            # has nothing to explore or change.
            import tempfile
            subprocess.run(["kimi", "-p", "Reply with the single word: ok"],
                           cwd=tempfile.mkdtemp(prefix="review-loop-kimi-"), capture_output=True)
            d = json.load(open(KIMI_CRED))
        return d["access_token"]


def adapt(j):
    """Make nanocode's request acceptable to the reviewer's API."""
    if REV_PROVIDER:
        j["provider"] = {"only": [REV_PROVIDER]}     # OpenRouter endpoint pin
    if "kimi.com" in REV_BASE_URL:
        # The Kimi API rejects nanocode's `exec` tool ("Only 'apply_patch' is supported"), so the
        # reviewer gets no tools at all: it reviews from the diff, and cannot write anything.
        j.pop("tools", None)
        j["input"] = [i for i in j.get("input", []) if i.get("type") != "additional_tools"]
    return j


def forwarder():
    """Loopback forwarder to REV_BASE_URL: signs each request and adapts its JSON body."""
    import http.client, http.server, urllib.parse
    up = urllib.parse.urlsplit(REV_BASE_URL)

    class H(http.server.BaseHTTPRequestHandler):   # HTTP/1.0: body ends at close, so streaming just works
        def do(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.dumps(adapt(json.loads(body))).encode()
            except ValueError:
                pass
            skip = ("host", "content-length", "accept-encoding", "connection", "transfer-encoding", "authorization")
            h = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            h["Authorization"] = "Bearer " + rev_token()
            c = http.client.HTTPSConnection(up.netloc, timeout=900)
            c.request(self.command, up.path + self.path, body or None, h)
            r = c.getresponse()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in skip:
                    self.send_header(k, v)
            self.end_headers()
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        do_GET = do_POST = do

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    REV_ENV.update(NANOCODE_OPENAI_BASE_URL="http://127.0.0.1:%d" % srv.server_port,
                   NANOCODE_OPENAI_API_KEY="set-by-forwarder")


# ---- findings ledger -------------------------------------------------------------------

def fid(f):
    norm = " ".join(re.sub(r"[^a-z0-9]+", " ", str(f["title"]).lower()).split())
    return hashlib.sha1(("%s|%s" % (f["file"], norm)).encode()).hexdigest()[:8]


def parse(reply):
    out = []
    for line in reply.splitlines():
        line = line.strip().strip("`").rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            f = json.loads(line)
        except ValueError:
            continue
        if isinstance(f, dict) and f.get("file") and f.get("title"):
            f["severity"] = f["severity"] if f.get("severity") in ("high", "medium", "low") else "medium"
            out.append(f)
    return out


def load():
    return json.load(open(LEDGER)) if os.path.exists(LEDGER) else []


def save(led):
    json.dump(led, open(LEDGER + ".tmp", "w"), indent=1)
    os.replace(LEDGER + ".tmp", LEDGER)


def merge(led, found, snap):
    """Add unseen findings to the ledger; returns the new ones."""
    seen, new = {f["id"] for f in led}, []
    for f in found:
        f["id"] = fid(f)
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        f.update(snap=snap, status="new" if f["severity"] in SEND else "logged")
        led.append(f)
        new.append(f)
    return new


def pending():
    with LEDGER_LOCK:
        return [f for f in load() if f["status"] == "new"]


def mark_sent(ids):
    with LEDGER_LOCK:
        led = load()
        for f in led:
            if f["id"] in ids:
                f["status"] = "sent"   # delivered exactly once: no feedback loop
        save(led)


# ---- reviewer --------------------------------------------------------------------------

def review(force=False, final=False):
    """Review reviewed..snapshot (or base..snapshot when final). Single flight."""
    global PREV, LAST_T
    with REVIEW_LOCK:
        snap = snapshot()
        quiet, PREV = snap == PREV, snap
        a = ref("base") if final else ref("reviewed")
        n = changed(a, snap)
        if not n or not (force or quiet and (n >= MIN_LINES or time.time() - LAST_T >= PERIOD)):
            return 0
        LAST_T = time.time()
        # Read-only by isolation: the reviewer only ever sees a detached, chmod a-w worktree.
        if not os.path.isdir(WT):
            git("worktree", "add", "--detach", WT, snap)
        subprocess.run(["chmod", "-R", "u+w", WT], check=True)
        git("checkout", "-f", "--detach", snap, cwd=WT)
        git("clean", "-fdq", cwd=WT)
        subprocess.run(["chmod", "-R", "a-w", WT], check=True)

        diff = git("diff", "-W", a, snap)          # -W: whole functions, the reviewer's only context
        if len(diff) > MAX_DIFF:
            diff = git("diff", a, snap)[:MAX_DIFF] + "\n[diff truncated]"
        with LEDGER_LOCK:
            known = "\n".join("- %s: %s" % (f["file"], f["title"]) for f in load()[-100:]) or "- (none)"
        prompt = REV_PROMPT.format(
            scope="the finished change; this is the final comprehensive review of the whole of it"
                  if final else "an incremental diff of work in progress",
            extra=", pieces that do not fit together, risky logic without any test" if final else "",
            wip="" if final else ", no notes about features that are simply not written yet",
            known=known, diff=diff)
        # ponytail: incremental reviews share one resumed session, so its history grows with
        # every diff; rotate .review/rev.session if reviewer token cost starts to matter.
        kind = "final" if final else "incremental"
        status("rev", "%s review %s..%s, %d lines" % (kind, a[:7], snap[:7], n), busy=True)
        rev_token()   # refresh an expiring login now, not inside a request nanocode is waiting on
        reply = nc("final" if final else "rev", WT, REV_MODEL, REV_THINK, prompt,
                   resume=not final, extra=("--no-subagents",), env=REV_ENV)
        with LEDGER_LOCK:
            led = load()
            new = merge(led, parse(reply), snap)
            save(led)
        git("update-ref", "refs/review/reviewed", snap)   # only advance on success
        status("rev", "idle")
        show("final" if final else "rev", "note", "%s review of %d lines: %s" % (
            kind, n, "%d new finding(s)" % len(new) if new else "nothing new"))
        emit(["    %s %s:%s  %s%s" % (c(SEV_COLOR[f["severity"]], "%-6s" % f["severity"]), f["file"],
                                    f.get("line", "?"), f["title"], c("2", "  (log only)") if f["status"] == "logged" else "")
              for f in new])
        return len(new)


def watch():
    while not STOP.wait(POLL):
        try:
            review()
        except Exception as e:   # keep developing; the forced review at TASK_DONE will surface it
            status("rev", "failed, retrying in 5 min")
            say("review failed, retry in 5 min: %s" % e)
            STOP.wait(300)


# ---- developer -------------------------------------------------------------------------

TURN = [0]


def dev(prompt):
    TURN[0] += 1
    status("dev", "turn %d" % TURN[0], busy=True)
    reply = nc("dev", ".", DEV_MODEL, DEV_THINK, prompt)
    status("dev", "idle")
    return reply


def dev_with_findings(intro=""):
    fs = pending()
    body = "".join("- [%s] %s %s:%s — %s. %s\n" % (f["id"], f["severity"], f["file"], f.get("line", "?"),
                                                   f["title"], f.get("detail", "")) for f in fs)
    if fs:
        say("sending %d finding(s) to Claude" % len(fs))
    reply = dev(intro + (DEV_FINDINGS + body if fs else "") or "Continue the task.")
    mark_sent({f["id"] for f in fs})
    return reply


def setup(repo):
    os.chdir(repo)
    git("rev-parse", "--git-dir")   # must be a git repo
    os.makedirs(D, exist_ok=True)
    open(D + "/.gitignore", "w").write("*\n")   # keeps .review out of snapshots and commits
    if os.path.exists(D + "/done"):
        sys.exit("already finished; delete .review/ to start over")


def save_task(task):
    if task and not os.path.exists(D + "/task"):
        open(D + "/task", "w").write(task)


def run(task):
    save_task(task)
    if not os.path.exists(D + "/task"):
        sys.exit("no task: pass one to start")
    if not ref("base"):
        s = snapshot()
        git("update-ref", "refs/review/base", s)
        git("update-ref", "refs/review/reviewed", s)

    forwarder()
    threading.Thread(target=watch, daemon=True).start()
    intro = "" if os.path.exists(D + "/dev.started") else DEV_INTRO.format(task=open(D + "/task").read()) + "\n\n"
    for _ in range(MAX_TURNS):
        reply, intro = dev_with_findings(intro), ""
        if reply.rstrip().endswith("TASK_DONE"):
            review(force=True)          # flush whatever is still unreviewed
            if not pending():
                break
    else:
        say("developer turn cap reached, going to final review")
    STOP.set()

    for r in range(FINAL_ROUNDS):       # round 0: whole change, fresh session; then only the fixes
        review(force=True, final=(r == 0))
        if not pending():
            break
        dev_with_findings()
    led = load()
    open(D + "/done", "w").write(time.strftime("%F %T\n"))
    status("dev", "finished")
    status("rev", "finished")
    say("finished: %d finding(s), %d sent to developer. Ledger: %s/%s" % (
        len(led), sum(f["status"] == "sent" for f in led), os.getcwd(), LEDGER))


# ---- interactive terminal --------------------------------------------------------------

def ask_task():
    """Inline prompt. Enter starts; a pasted multi-line task arrives as several lines at once."""
    import select
    rule = c("2", "─" * min(width(), 100))
    print("\n".join([c("1", "claude-review") + c("2", "  " + os.getcwd()),
                     c("2", "What should Claude do? Enter to start, ctrl-c to quit."), rule]))
    sys.stdout.write(c("38;5;209", "❯ "))
    sys.stdout.flush()
    buf = os.read(0, 65536)
    while buf and select.select([0], [], [], 0.05)[0]:
        buf += os.read(0, 65536)
    print(rule)
    return buf.decode(errors="replace").strip()


def interactive(task):
    import termios
    if not task and not os.path.exists(D + "/task"):
        task = ask_task()
        if not task:
            return
    save_task(task)
    show("loop", "note", "Task: " + open(D + "/task").read().strip())
    err = []

    def work():
        try:
            run(None)
        except Exception as e:
            err.append("%s" % (getattr(e, "stderr", "") or e))
    worker = threading.Thread(target=work, daemon=True)
    fd, old = 0, termios.tcgetattr(0)
    quiet = termios.tcgetattr(0)
    quiet[3] &= ~termios.ECHO                       # keystrokes would garble the status line
    termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
    sys.stdout.write("\x1b[?25l")                   # hide cursor
    try:
        worker.start()
        while worker.is_alive():
            worker.join(0.15)
            emit([])                                # animate the status line
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        emit([], redraw=False)
        sys.stdout.write("\x1b[?25h")
    if worker.is_alive():
        for p in list(KIDS):
            p.terminate()   # nanocode journals the turn; a rerun resumes it
        sys.exit("stopped; rerun claude-review in %s to resume" % os.getcwd())
    if err:
        sys.exit("claude-review: %s\nState is saved; rerun the same command to resume." % err[-1])


def selftest():
    reply = 'noise\n```\n{"file":"a.py","line":3,"severity":"high","title":"SQL injection!","detail":"x"}\n' \
            '{"file":"a.py","title":"sql  Injection","severity":"bogus"}\n{"file":"b.py","title":"t","severity":"low"}\n{bad\n```\nNONE'
    led = []
    assert len(merge(led, parse(reply), "s1")) == 2, led       # 2nd is a duplicate of the 1st
    assert [f["status"] for f in led] == ["new", "logged"]      # low severity is not fed back
    assert merge(led, parse(reply), "s2") == []                 # re-reporting adds nothing
    assert parse("NONE") == []
    print("selftest ok")


def cli():
    if sys.argv[1:] == ["--selftest"]:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("task", nargs="?")
    ap.add_argument("-C", dest="repo", default=".")
    args = ap.parse_args()
    try:
        setup(args.repo)
        if sys.stdin.isatty() and sys.stdout.isatty():
            return interactive(args.task)
        run(args.task)
    except (RuntimeError, subprocess.CalledProcessError) as e:
        sys.exit("claude-review: %s\nState is saved; rerun the same command to resume." % (getattr(e, "stderr", "") or e))


if __name__ == "__main__":
    cli()
