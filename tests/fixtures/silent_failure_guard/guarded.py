"""Silent-failure guard fixture: code the rule must accept (tests/test_silent_failure_guard.py). Never imported."""

import logging

log = logging.getLogger(__name__)


def pair(labels, scores):
    return list(zip(labels, scores, strict=True))  # a length mismatch raises


def window(xs):
    return list(zip(xs, xs[1:], strict=False))  # shorter by design: says so


def load(paths, read):
    out = []
    for p in paths:
        try:
            out.append(read(p))
        except OSError as exc:
            log.warning("skipping %s: %s", p, exc)
    return out


def vram_free(torch):
    try:
        return int(torch.cuda.mem_get_info()[0])
    except Exception:  # noqa: S110 best-effort health probe: None means unknown
        pass
    return None


def narrow(fn):
    try:
        return fn()
    except KeyError:
        pass  # a specific, expected exception is not this rule's concern


def load_state(path, read_json):
    try:
        return read_json(path)
    except Exception:  # noqa: S110 the caller treats {} as "start a new state" and logs that
        return {}


def lookup(fn):
    try:
        return fn()
    except Exception:
        return fn.default  # a real fallback value, not an empty one: outside this rule


def narrow_empty(read):
    try:
        return read()
    except FileNotFoundError:
        return {}


def suppress_narrow(fn, contextlib):
    with contextlib.suppress(FileNotFoundError):
        fn()


def suppress_reasoned(fn, contextlib):
    with contextlib.suppress(Exception):  # noqa: S110 closing a dead socket, nothing to lose
        fn()


def reset_and_log(read):
    try:
        state = read()
    except Exception:
        log.exception("state unreadable, starting empty")
        state = {}
    return state


def reset_narrow(read):
    try:
        state = read()
    except FileNotFoundError:
        state = {}
    return state


def real_fallback(read, default):
    try:
        state = read()
    except Exception:
        state = default
    return state
