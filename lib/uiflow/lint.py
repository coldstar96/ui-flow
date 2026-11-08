from collections import namedtuple

from uiflow import timing

SWIPE = "swipe"
SCROLL_TO = "scrollTo"
SLEEP = "sleep"
TAP_AT = "tapAt"
DEFAULT_SWIPE = "up"
VERTICAL = ("up", "down")
SELECTOR_FIELDS = ("id", "idPrefix", "label", "labelContains", "labelAnyOf",
                   "labelContainsAnyOf", "valueEquals", "type")

# Deliberately wider than timing.CHILD_KEYS, which is ("steps",): timing walks `steps` alone
# because that is the index space the driver reports results in, while a probe list is exactly
# where an unnoticed cost hides — nothing ever reports a reassert's wall clock.
CHILD_KEYS = ("steps", "satisfiedWhen", "verifies", "reassert", "until")

# Measured on iOS 26.5: scrollTo aborts after two swipes that leave the match count unchanged, so
# deleting a long run can turn a passing record into a failing one when the target is off-tree.
CAVEAT = ("Replay the flow after this edit: scrollTo gives up after two swipes that do not change "
          "the match count, so a target absent from the tree until several swipes in is never "
          "reached.")

Finding = namedtuple("Finding", ("rule", "steps", "message"))


def check(steps, satisfied_when=(), verifies=()):
    """Findings for steps that cost wall clock and can never fail, or that are silently wrong on
    another device — in both cases nothing else ever notices them.

    Pure over the steps, so an audit over already-saved records can call it too. Probe lists are
    linted under their own path prefix, and `steps` paths read exactly as timing keys do.
    """
    found = []
    _scan(steps, (), found)
    _scan(satisfied_when, ("satisfiedWhen",), found)
    _scan(verifies, ("verifies",), found)
    found.sort(key=lambda item: _order(item[0]))
    return [finding for _, finding in found]


def _scan(steps, prefix, found):
    steps = _as_steps(steps)
    covered = _redundant_before_scroll(steps, prefix, found)
    _repeated_swipes(steps, prefix, found, covered)
    for index, step in enumerate(steps):
        if _op(step) == SLEEP:
            found.append((prefix + (index,), _bare_sleep(prefix + (index,), step)))
    for index, step in enumerate(steps):
        if isinstance(step, dict) and _op(step) == TAP_AT and not step.get("sel"):
            found.append((prefix + (index,), _bare_tap_at(prefix + (index,))))
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        for key in CHILD_KEYS:
            child = _as_steps(step.get(key))
            if child:
                _scan(child, prefix + (index,) if key == "steps" else prefix + (index, key), found)


def _redundant_before_scroll(steps, prefix, found):
    covered = set()
    for index, step in enumerate(steps):
        if _op(step) != SCROLL_TO or index == 0 or not _plain_vertical_swipe(steps[index - 1]):
            continue
        if not _same_app(steps[index - 1], step):
            continue
        direction = _direction(steps[index - 1])
        start = index - 1
        while (start > 0 and _plain_vertical_swipe(steps[start - 1])
               and _direction(steps[start - 1]) == direction
               and _same_app(steps[start - 1], step)):
            start -= 1
        found.append((prefix + (start,), _redundant(prefix, start, index, direction, step)))
        covered.update(range(start, index))
    return covered


def _repeated_swipes(steps, prefix, found, covered):
    index = 0
    while index < len(steps):
        if not _plain_vertical_swipe(steps[index]):
            index += 1
            continue
        end = index
        while (end + 1 < len(steps) and _plain_vertical_swipe(steps[end + 1])
               and timing.fingerprint(steps[end + 1]) == timing.fingerprint(steps[index])):
            end += 1
        target = _next_target(steps, end + 1, steps[index])
        if end > index and target and not covered & set(range(index, end + 1)):
            found.append((prefix + (index,), _repeated(prefix, index, end, steps[index], target)))
        index = end + 1


def _redundant(prefix, start, scroll_index, direction, scroll):
    count = scroll_index - start
    scroll_key = timing.path_key(prefix + (scroll_index,))
    target = _selector_text(scroll.get("sel")) or "its selector"
    them = "the swipe" if count == 1 else "the %d swipes" % count
    if _direction(scroll) == direction:
        advice = ("which swipes %s itself until %s resolves and stops there — delete %s, %s wall "
                  "clock the scrollTo was going to spend anyway. %s"
                  % (direction, target, them, "it is" if count == 1 else "they are", CAVEAT))
    else:
        advice = ("which swipes %s instead, away from where %s the screen — delete %s and add "
                  "\"direction\": \"%s\" to the scrollTo at step %s, which then swipes %s itself "
                  "and stops the moment %s resolves. %s"
                  % (_direction(scroll), "it leaves" if count == 1 else "they leave", them,
                     direction, scroll_key, direction, target, CAVEAT))
    return Finding(
        "redundant-swipe-before-scrollTo",
        _keys(prefix, range(start, scroll_index)) + (scroll_key,),
        "%s %s immediately before the scrollTo at step %s, %s"
        % (_run_phrase(prefix, start, scroll_index - 1), direction, scroll_key, advice),
    )


def _repeated(prefix, start, end, step, target):
    return Finding(
        "repeated-swipe",
        _keys(prefix, range(start, end + 1)),
        "steps %s-%s are %d identical %s swipes — replace them with one scrollTo carrying "
        "\"direction\": \"%s\" and the selector the next resolving step names (%s). scrollTo swipes "
        "itself and stops the moment the target resolves, so these cost their whole wall clock "
        "every run and can never fail, which is why nothing else will ever flag them. %s"
        % (timing.path_key(prefix + (start,)), timing.path_key(prefix + (end,)), end - start + 1,
           _direction(step), _direction(step), target, CAVEAT),
    )


def _bare_tap_ats(steps, prefix=()):
    found = []
    for index, step in enumerate(_as_steps(steps)):
        if not isinstance(step, dict):
            continue
        path = prefix + (index,)
        if _op(step) == TAP_AT and not step.get("sel"):
            found.append(path)
        for key in CHILD_KEYS:
            child = _as_steps(step.get(key))
            if child:
                found.extend(_bare_tap_ats(
                    child, path if key == "steps" else path + (key,)
                ))
    return found


def shape_dependent(steps, satisfied_when=(), verifies=()):
    """So a caller can warn about a device change only when the record actually depends on one."""
    found = _bare_tap_ats(steps)
    found.extend(_bare_tap_ats(satisfied_when, ("satisfiedWhen",)))
    found.extend(_bare_tap_ats(verifies, ("verifies",)))
    return tuple(timing.path_key(path) for path in found)


def _bare_tap_at(path):
    key = timing.path_key(path)
    return Finding(
        "device-shaped-tapAt",
        (key,),
        "step %s is a tapAt with no sel, so its dx/dy offset is taken inside the app WINDOW — at a "
        "different screen shape that fraction lands on a different control. Give it a sel so the "
        "offset resolves against a real element, which re-resolves on every replay." % key,
    )


def _bare_sleep(path, step):
    reassert = " In a reassert it is paid again after every focus-moving step in the group." \
        if "reassert" in path else ""
    return Finding(
        "bare-sleep",
        (timing.path_key(path),),
        "step %s is a bare sleep of %s ms — a wall-clock wait is paid in full every run whether or "
        "not the screen was ready, and is too short the one time it matters.%s Assert on the thing "
        "being waited for instead; waitFor returns the moment it appears."
        % (timing.path_key(path), step.get("ms", 100), reassert),
    )


def _run_phrase(prefix, start, end):
    if start == end:
        return "step %s swipes" % timing.path_key(prefix + (start,))
    return "steps %s-%s swipe" % (timing.path_key(prefix + (start,)),
                                  timing.path_key(prefix + (end,)))


def _next_target(steps, index, run_step):
    """Only the step immediately after the run: anything later is reached through an intervening
    step, so naming its selector would rewrite the flow rather than shorten it."""
    if index >= len(steps):
        return None
    step = steps[index]
    if not isinstance(step, dict) or not isinstance(step.get("sel"), dict):
        return None
    return _selector_text(step["sel"]) if _same_app(step, run_step) else None


def _plain_vertical_swipe(step):
    """A swipe carrying a `sel` is executed against THAT element, not the app's first scroll
    container, so it is a different gesture from anything scrollTo could do."""
    return (_op(step) == SWIPE and step.get("sel") is None and _direction(step) in VERTICAL)


def _as_steps(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def _keys(prefix, indices):
    return tuple(timing.path_key(prefix + (index,)) for index in indices)


def _order(path):
    return tuple((0, element, "") if isinstance(element, int) else (1, 0, element) for element in path)


def _op(step):
    return step.get("op") if isinstance(step, dict) else None


def _app(step):
    if not isinstance(step, dict):
        return None
    selector = step.get("sel")
    return step.get("app") or (selector.get("app") if isinstance(selector, dict) else None)


def _same_app(one, other):
    first, second = _app(one), _app(other)
    return first is None or second is None or first == second


def _direction(step):
    if not isinstance(step, dict):
        return DEFAULT_SWIPE
    if step.get("op") == SCROLL_TO:
        # scrollTo is binary in the driver: "down" swipes down, ANYTHING else swipes up.
        return "down" if step.get("direction") == "down" else "up"
    return step.get("direction") or DEFAULT_SWIPE


def _selector_text(selector):
    if not isinstance(selector, dict):
        return None
    for field in SELECTOR_FIELDS:
        value = selector.get(field)
        if value:
            return "%s=%s" % (field, value if not isinstance(value, list) else "|".join(value))
    return _selector_text(selector.get("descendant"))
