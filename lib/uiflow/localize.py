import copy

from uiflow import compose

LABEL_FIELDS = (("label", "labelAnyOf"), ("labelContains", "labelContainsAnyOf"))
TRAILING = " ,:.…"


def _alternatives(label, translate):
    """Translations for a label, falling back to the fragment without its trailing punctuation.

    An accessibility label is composed (`Keyboards, 3`), so a `labelContains` fragment is often not
    a catalogue string at all — `Keyboards,` is in no `.strings` file, `Keyboards` is.
    """
    found = [value for value in translate(label) if value and value != label]
    if found:
        return found
    trimmed = label.rstrip(TRAILING)
    if trimmed and trimmed != label:
        # `value != trimmed`: an alternative equal to the fragment minus its punctuation matches
        # strictly more rows than the record asked for.
        return [value for value in translate(trimmed) if value and value != trimmed]
    return []


def expand(selector, translate):
    """`label` -> `labelAnyOf` [english, *translations]. English stays first, so a miss degrades to
    exactly today's behavior instead of failing the batch."""
    if not isinstance(selector, dict):
        return selector, [], 0
    expanded = dict(selector)
    missing = []
    seen = 0
    for source, target in LABEL_FIELDS:
        label = expanded.get(source)
        if not isinstance(label, str) or not label:
            continue
        seen += 1
        alternatives = _alternatives(label, translate)
        if not alternatives:
            missing.append(label)
            continue
        del expanded[source]
        expanded[target] = [label] + alternatives
    inner = expanded.get("descendant")
    if isinstance(inner, dict):
        expanded["descendant"], nested, counted = expand(inner, translate)
        missing.extend(nested)
        seen += counted
    return expanded, missing, seen


def canonicalize(selector, oracle):
    """The reverse: whatever a localized screen showed, mapped back to the English a record stores.

    Only a string the catalogue KNOWS as a localized value is touched. "Is this English?" is the
    wrong question — `iter_pairs` drops identity pairs, so `Bluetooth`, `AirDrop` and every carried
    English fragment are absent from the `en` column and answering it discarded correct repairs.

    Returns the mapped selector and the labels it refused to guess at: a localized string with more
    than one English original, paired with its candidates.
    """
    if not oracle or not isinstance(selector, dict):
        return selector, []
    mapped = dict(selector)
    ambiguous = []
    for source, target in LABEL_FIELDS:
        options = mapped.get(target)
        if isinstance(options, list) and options:
            # `expand` builds the alternation english-first, and it is the only thing that builds
            # one, so element 0 IS the original.
            del mapped[target]
            mapped[source] = options[0]
            continue
        label = mapped.get(source)
        if not isinstance(label, str) or not label:
            continue
        if oracle.knows_english(label) or not oracle.knows_localized(label):
            continue
        english = oracle.canonical(label)
        if english:
            mapped[source] = english
        else:
            ambiguous.append((label, tuple(oracle.originals(label))))
    inner = mapped.get("descendant")
    if isinstance(inner, dict):
        mapped["descendant"], nested = canonicalize(inner, oracle)
        ambiguous.extend(nested)
    return mapped, ambiguous


def map_selectors(steps, fn):
    mapped = []
    for step in steps or []:
        current = dict(step)
        if isinstance(current.get("sel"), dict):
            current["sel"] = fn(current["sel"])
        if isinstance(current.get("sels"), list):
            current["sels"] = [fn(one) if isinstance(one, dict) else one for one in current["sels"]]
        for key in compose.STEP_KEYS:
            if isinstance(current.get(key), list):
                current[key] = map_selectors(current[key], fn)
        mapped.append(current)
    return mapped


def localize(steps, translate):
    missing = []
    counted = [0]

    def rewrite(selector):
        expanded, absent, seen = expand(selector, translate)
        missing.extend(absent)
        counted[0] += seen
        return expanded

    return map_selectors(copy.deepcopy(steps or []), rewrite), sorted(set(missing)), counted[0]


def canonicalize_steps(steps, oracle):
    ambiguous = []

    def rewrite(selector):
        mapped, found = canonicalize(selector, oracle)
        ambiguous.extend(found)
        return mapped

    return map_selectors(copy.deepcopy(steps or []), rewrite), sorted(set(ambiguous))
