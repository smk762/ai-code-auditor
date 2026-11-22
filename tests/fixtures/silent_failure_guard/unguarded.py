"""Silent-failure guard fixture: code the rule must flag (tests/test_silent_failure_guard.py). Never imported."""


def pair(labels, scores):
    return list(zip(labels, scores))  # a short list silently drops the rest


def load(paths, read):
    out = []
    for p in paths:
        try:
            out.append(read(p))
        except Exception:
            continue  # the failure vanishes
    return out


def probe(fn):
    try:
        return fn()
    except:  # noqa: E722
        pass


def unexplained(fn):
    try:
        return fn()
    except BaseException:  # noqa: S110
        pass
