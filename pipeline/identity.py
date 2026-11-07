"""Archer identity resolution.

Goal: a stable ``archer_id`` so one person's results can be followed across
tournaments, years, divisions and name spellings, without merging different
people who merely share a name (there are many: e.g. three different
"Justin Kim"s in a single event).

Evidence the design is built on (see docs/METHODOLOGY.md):
  * Every archer record carries ``alt`` - a numeric person-level ID present on
    ~99% of records (4-6 digits; believed to be the USA Archery membership
    number). The same person entered twice in one event (two divisions, or
    "Archie Nixon - LB") shares one ``alt``; same-name homonyms do not.
  * ``aid`` is only an event-local registration row, never a person key.

Rules, in order:
  1. A record with a *valid* alt joins archer ``A<alt>``.
       - If one alt carries clearly incompatible names (different last name),
         the alt is split into one identity per name cluster ("A<alt>", "A<alt>~2" ...)
         and flagged in the review table.
       - An alt is "valid" if it is 3-7 digits, not an obvious placeholder, and
         is not shared by an implausibly large number of distinct names.
  2. A record with no valid alt joins an existing alt-identity only when its exact
     normalised name belongs to exactly ONE alt-identity (unambiguous).
  3. Otherwise it becomes a name-only identity ``N<hash(name_key)>`` (all records
     with that name and no alt merge together; confidence is flagged lower).
  5. Name-bridge pass (``name_bridge``, labelled ``name_bridged`` = medium confidence): a leftover
     fragment (other-system id, 3-digit sequence number, or blank id) joins an archer only if
     exactly ONE strong-id archer has that exact name, genders are compatible, the fragment never
     appears in the same event under a different id, and there is no youth/adult conflict in
     overlapping years.
  4. Bridge pass (``bridge``): ``alt`` is the ID *within whichever registration
     system the event used*. 5-6 digit values are stable USA Archery member numbers
     ("strong"). Other lengths ("weak": 3-4 digits, 7 digits) are re-issued per
     season / per organisation - e.g. one international guest appears under 14
     different 4-digit alts over 2018-2026, and under two alts in a single
     tournament. Weak identities are merged into a person only with positive
     evidence (same name, compatible gender, overlapping club, or - when a club is
     missing - an unambiguous division match). Two strong identities are never
     merged (they are different people or we cannot tell), and any ambiguity is
     left split and written to the review table.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_PLACEHOLDER_ALTS = {
    "0", "00", "000", "0000", "00000", "000000", "1", "11", "111", "1111", "11111", "111111",
    "123", "1234", "12345", "123456", "1234567", "999999", "99999", "9999", "99999999",
}
# An alt shared by more than this many *incompatible* name clusters is not a person id.
MAX_NAME_CLUSTERS_PER_ALT = 4

_ANNOT_RX = re.compile(r"\s+[-–—]\s+[A-Za-z0-9]{1,8}$")  # "Archie Nixon - LB"
_PAREN_RX = re.compile(r"\([^)]*\)")


def normalize_alt(alt) -> str | None:
    """Digits-only person id, or None if blank / malformed / placeholder."""
    if alt is None:
        return None
    s = str(alt).strip()
    if not s or not s.isdigit():
        return None
    s = s.lstrip("0") or "0"
    if not (3 <= len(s) <= 7) or s in _PLACEHOLDER_ALTS:
        return None
    return s


def clean_display_name(first: str | None, last: str | None) -> str:
    first = (first or "").strip()
    last = (last or "").strip()
    full = f"{first} {last}".strip()
    full = _PAREN_RX.sub(" ", full)
    full = _ANNOT_RX.sub("", full)
    full = re.sub(r"\s+", " ", full).strip(" -")
    if full and (full.isupper() or full.islower()):
        full = " ".join(_cap(w) for w in full.split(" "))
    return full


def _cap(w: str) -> str:
    # Title-case but keep Mc/Mac/O'/De-style handled reasonably.
    w = w.capitalize()
    w = re.sub(r"^(Mc)([a-z])", lambda m: m.group(1) + m.group(2).upper(), w)
    w = re.sub(r"^(O')([a-z])", lambda m: m.group(1) + m.group(2).upper(), w)
    return w


def name_key(first: str | None, last: str | None) -> str:
    """ASCII, lowercase, punctuation-free key (generational suffixes are kept)."""
    full = clean_display_name(first, last)
    full = unicodedata.normalize("NFKD", full).encode("ascii", "ignore").decode()
    full = full.lower().replace("'", "")
    full = re.sub(r"[^a-z0-9 ]+", " ", full)
    return re.sub(r"\s+", " ", full).strip()


def _tokens(key: str) -> list[str]:
    return key.split()


_NICK = {frozenset(p) for p in [
    ("mike", "michael"), ("bill", "william"), ("will", "william"), ("billy", "william"), ("bob", "robert"), ("rob", "robert"),
    ("bobby", "robert"), ("jim", "james"), ("jimmy", "james"), ("tom", "thomas"), ("tommy", "thomas"), ("dave", "david"),
    ("matt", "matthew"), ("chris", "christopher"), ("chris", "christian"), ("ben", "benjamin"), ("sam", "samuel"),
    ("sam", "samantha"), ("alex", "alexander"), ("alex", "alexandra"), ("alex", "alexis"), ("kate", "katherine"),
    ("katie", "katherine"), ("kathy", "katherine"), ("liz", "elizabeth"), ("beth", "elizabeth"), ("abby", "abigail"),
    ("maggie", "margaret"), ("becky", "rebecca"), ("bella", "isabella"), ("izzy", "isabella"), ("jen", "jennifer"),
    ("jenny", "jennifer"), ("josh", "joshua"), ("nick", "nicholas"), ("nate", "nathan"), ("nate", "nathaniel"),
    ("zach", "zachary"), ("zack", "zachary"), ("andy", "andrew"), ("drew", "andrew"), ("tony", "anthony"), ("ed", "edward"),
    ("eddie", "edward"), ("ted", "edward"), ("dan", "daniel"), ("danny", "daniel"), ("joe", "joseph"), ("jake", "jacob"),
    ("hank", "henry"), ("steve", "steven"), ("steve", "stephen"), ("pat", "patrick"), ("tim", "timothy"),
    ("sue", "susan"), ("jess", "jessica"), ("jessie", "jessica"), ("sophie", "sophia"), ("cam", "cameron"),
    ("brad", "bradley"), ("greg", "gregory"), ("jeff", "jeffrey"), ("ken", "kenneth"), ("larry", "lawrence"),
    ("rick", "richard"), ("rich", "richard"), ("ron", "ronald"), ("don", "donald"), ("doug", "douglas"),
    ("fred", "frederick"), ("gabe", "gabriel"), ("gabby", "gabrielle"), ("lexi", "alexis"), ("maddie", "madison"),
    ("maddy", "madison"), ("lizzy", "elizabeth"), ("jon", "jonathan"), ("johnny", "john"), ("jack", "john"),
]}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _strip_suffix(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _SUFFIXES] or tokens


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _first_similar(f1: str, f2: str) -> bool:
    if f1 == f2 or frozenset((f1, f2)) in _NICK:
        return True
    if min(len(f1), len(f2)) >= 3 and (f1 in f2 or f2 in f1):      # bella / isabella
        return True
    if len(f1) == 1 or len(f2) == 1:                                 # initial
        return f1[0] == f2[0]
    return _ratio(f1, f2) >= 0.8                                     # mackenzie / makenzie, benajmin / benjamin


def _last_similar(l1: str, l2: str) -> bool:
    return l1 == l2 or _ratio(l1, l2) >= 0.86                        # mcclain / mclain


def names_compatible(a: str, b: str) -> bool:
    """Do two name keys plausibly refer to the same person? (used where an ID is shared)

    Tolerates spelling slips, nicknames, middle names, generational suffixes and swapped
    first/last. Deliberately does NOT treat 'same first name, different surname' as compatible:
    that is a marriage/name change OR two different people on one number, and we cannot tell.
    """
    if a == b:
        return True
    ta, tb = _strip_suffix(_tokens(a)), _strip_suffix(_tokens(b))
    if not ta or not tb:
        return False
    if sorted(ta) == sorted(tb):
        return True
    if _last_similar(ta[-1], tb[-1]) and _first_similar(ta[0], tb[0]):
        return True
    return set(ta) <= set(tb) or set(tb) <= set(ta)     # middle names: "erev mhb richards" / "erev richards"


STRONG_ALT_LENGTHS = (5, 6)

def _is_strong(cid: str, marquee_seen: bool, n_years: int) -> bool:
    """A stable USA Archery member number: 5-6 digits AND corroborated - seen in a marquee USAA
    event (those require a USAA membership) or persisting across 2+ calendar years. Numbers that
    only ever appear in a local/other-organisation series are NOT trusted as person ids."""
    if not cid.startswith("A"):
        return False
    digits = re.sub(r"\D", "", cid.split("~")[0])
    return len(digits) in STRONG_ALT_LENGTHS and (marquee_seen or n_years >= 2)

_EMPTY_CLUBS = {"", "-", "none", "n/a", "na", "unattached", "unaffiliated", "independent", "nan"}


def _norm_club(c) -> str:
    c = re.sub(r"[^a-z0-9 ]+", " ", str(c or "").lower())
    c = re.sub(r"\s+", " ", c).strip()
    return "" if c in _EMPTY_CLUBS else c


@dataclass
class _Cl:
    cid: str
    key: str
    strong: bool
    genders: set
    clubs: set
    divisions: set
    tournaments: set
    n: int = 0


def _gender_ok(a: set, b: set) -> bool:
    ga, gb = a - {"?", "X", ""}, b - {"?", "X", ""}
    return not ga or not gb or bool(ga & gb)


def _compat(a: _Cl, b: _Cl) -> bool:
    """Positive evidence that two identity clusters with the same name are one person."""
    if not _gender_ok(a.genders, b.genders):
        return False
    if a.clubs and b.clubs:
        return bool(a.clubs & b.clubs)
    # a side has no club information: need an unambiguous shared division AND no tournament overlap
    return bool(a.divisions & b.divisions) and not (a.tournaments & b.tournaments)


def bridge(ids, keys, genders, clubs, divisions, tournaments, marquee=None, years=None):
    """Merge weak-alt identities into one person where the evidence supports it.

    Returns (mapping base_id -> final_id, review rows). Only ``A...`` identities are considered.
    """
    cl: dict[str, _Cl] = {}
    key_votes: dict[str, Counter] = defaultdict(Counter)
    mq_seen: dict[str, bool] = defaultdict(bool)
    yrs_seen: dict[str, set] = defaultdict(set)
    for i, cid in enumerate(ids):
        if cid.startswith("A"):
            if marquee is not None and marquee[i]:
                mq_seen[cid] = True
            if years is not None and years[i] is not None:
                yrs_seen[cid].add(years[i])
    for i, cid in enumerate(ids):
        if not cid.startswith("A"):
            continue
        key_votes[cid][keys[i]] += 1
        c = cl.get(cid)
        if c is None:
            strong = (_is_strong(cid, mq_seen[cid], len(yrs_seen[cid])) if (marquee is not None or years is not None)
                      else len(re.sub(r"\D", "", cid.split("~")[0])) in STRONG_ALT_LENGTHS)
            c = cl[cid] = _Cl(cid, "", strong, set(), set(), set(), set())
        c.genders.add(genders[i] or "?")
        cc = _norm_club(clubs[i])
        if cc:
            c.clubs.add(cc)
        if divisions[i]:
            c.divisions.add(divisions[i])
        c.tournaments.add(tournaments[i])
        c.n += 1
    for cid, c in cl.items():
        c.key = key_votes[cid].most_common(1)[0][0]

    by_key: dict[str, list[_Cl]] = defaultdict(list)
    for c in cl.values():
        by_key[c.key].append(c)

    mapping: dict[str, str] = {}
    review: list[dict] = []
    for key, group in by_key.items():
        if not key or len(group) < 2:
            continue
        anchors = [c for c in group if c.strong]
        weaks = sorted([c for c in group if not c.strong], key=lambda c: c.cid)
        if not weaks:
            continue
        # union-find over weak clusters: clusters WITH club info link by club overlap first
        parent = {w.cid: w.cid for w in weaks}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        with_club = [w for w in weaks if w.clubs]
        for i, a in enumerate(with_club):
            for b in with_club[i + 1:]:
                if _gender_ok(a.genders, b.genders) and (a.clubs & b.clubs):
                    parent[find(a.cid)] = find(b.cid)
        groups: dict[str, list[_Cl]] = defaultdict(list)
        for w in with_club:
            groups[find(w.cid)].append(w)

        def merged(members):
            m = _Cl(min(x.cid for x in members), key, False, set(), set(), set(), set())
            for x in members:
                m.genders |= x.genders; m.clubs |= x.clubs; m.divisions |= x.divisions; m.tournaments |= x.tournaments; m.n += x.n
            return m

        wgroups = [merged(v) for v in groups.values()]
        wmembers = {merged(v).cid: v for v in groups.values()}
        # weak clusters with no club info attach to a unique compatible target (club group or anchor)
        for w in [w for w in weaks if not w.clubs]:
            targets = [t for t in wgroups + anchors if _compat(w, t)]
            if len(targets) == 1:
                t = targets[0]
                if t.strong:
                    mapping[w.cid] = t.cid
                else:
                    wmembers[t.cid].append(w)
                    t.divisions |= w.divisions
                    t.tournaments |= w.tournaments
            else:
                wgroups_after = None  # ambiguous or unsupported: stay split
                if len(targets) > 1:
                    review.append({"kind": "bridge_ambiguous", "alt": w.cid, "names": key,
                                   "detail": f"weak id compatible with {len(targets)} identities: {[t.cid for t in targets][:4]}"})
        # attach each weak club-group to an anchor only when exactly one anchor is compatible
        for t in list(wgroups):
            members = wmembers[t.cid]
            cands = [a for a in anchors if _compat(t, a)]
            if len(cands) == 1:
                for x in members:
                    mapping[x.cid] = cands[0].cid
            else:
                final = min(x.cid for x in members)
                for x in members:
                    if x.cid != final:
                        mapping[x.cid] = final
                if len(cands) > 1:
                    review.append({"kind": "bridge_ambiguous", "alt": ",".join(x.cid for x in members), "names": key,
                                   "detail": f"weak group compatible with several strong ids: {[a.cid for a in cands][:4]}"})
        for w, tgt in list(mapping.items()):
            pass
    # resolve chains
    def final_of(x):
        seen = set()
        while x in mapping and x not in seen:
            seen.add(x); x = mapping[x]
        return x

    mapping = {k: final_of(k) for k in mapping}
    merged_groups: dict[str, list[str]] = defaultdict(list)
    for k, v in mapping.items():
        merged_groups[v].append(k)
    for tgt, members in merged_groups.items():
        review.append({"kind": "bridged", "alt": ",".join(sorted(members + [tgt])), "names": cl[tgt].key if tgt in cl else "",
                       "detail": f"{len(members) + 1} weak/strong ids merged on name + club/gender/division evidence"})
    return mapping, review


_YOUTH = {"U13", "U15", "U18", "U21", "Youth", "Jr Eagle", "Eagle", "Elementary", "Middle School", "High School"}
_ADULT = {"Senior", "50+", "60+", "70+"}


def name_bridge(ids, keys, genders, events, age_classes, years, marquee=None):
    """Attach leftover fragments to the single strong-id archer sharing their exact name."""
    st: dict[str, dict] = {}
    votes: dict[str, Counter] = defaultdict(Counter)
    mq_seen: dict[str, bool] = defaultdict(bool)
    yrs_seen: dict[str, set] = defaultdict(set)
    for i, cid in enumerate(ids):
        if cid.startswith("A"):
            if marquee is not None and marquee[i]:
                mq_seen[cid] = True
            if years[i] is not None:
                yrs_seen[cid].add(years[i])
    for i, cid in enumerate(ids):
        d = st.get(cid)
        if d is None:
            d = st[cid] = {"strong": _is_strong(cid, mq_seen[cid], len(yrs_seen[cid])), "g": set(), "ev": set(),
                           "age": set(), "y0": 9999, "y1": 0}
        votes[cid][keys[i]] += 1
        d["g"].add(genders[i] or "?")
        d["ev"].add(events[i])
        d["age"].add(age_classes[i])
        if years[i] is not None:
            d["y0"], d["y1"] = min(d["y0"], years[i]), max(d["y1"], years[i])
    by_key: dict[str, list[str]] = defaultdict(list)
    for cid in st:
        by_key[votes[cid].most_common(1)[0][0]].append(cid)

    mapping: dict[str, str] = {}
    review: list[dict] = []
    for key, group in by_key.items():
        if not key or len(group) < 2:
            continue
        anchors = [c for c in group if st[c]["strong"]]
        if len(anchors) != 1:
            continue
        a = anchors[0]
        A = st[a]
        for c in group:
            if c == a or st[c]["strong"]:
                continue
            C = st[c]
            if not _gender_ok(A["g"], C["g"]):
                continue
            if A["ev"] & C["ev"]:                 # same event, different ids -> treat as different people
                continue
            overlap = C["y0"] <= A["y1"] and A["y0"] <= C["y1"]
            a_youth = A["age"] <= _YOUTH | {"Unknown", "Collegiate"} and bool(A["age"] & _YOUTH)
            c_youth = C["age"] <= _YOUTH | {"Unknown", "Collegiate"} and bool(C["age"] & _YOUTH)
            a_adult = bool(A["age"] & _ADULT) and not (A["age"] & _YOUTH)
            c_adult = bool(C["age"] & _ADULT) and not (C["age"] & _YOUTH)
            if overlap and ((a_youth and c_adult) or (a_adult and c_youth)):
                continue
            mapping[c] = a
    for tgt in sorted(set(mapping.values())):
        members = sorted(k for k, v in mapping.items() if v == tgt)
        review.append({"kind": "name_bridged", "alt": ",".join(members + [tgt]), "names": votes[tgt].most_common(1)[0][0],
                       "detail": f"{len(members)} fragment(s) joined the only strong-id archer with this exact name (medium confidence)"})
    return mapping, review


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:10]


@dataclass
class Resolution:
    archer_id: list[str]
    basis: list[str]          # alt | name_matched_to_alt | name_only
    review: list[dict] = field(default_factory=list)


def resolve(first_names, last_names, alts, clubs=None, years=None, genders=None, divisions=None,
            tournaments=None, events=None, age_classes=None, marquee=None) -> Resolution:
    """Resolve identities for parallel sequences of records. Deterministic.

    ``genders``/``divisions``/``tournaments`` (parallel sequences, optional) enable the bridge pass
    that merges re-issued weak alts; without them only rules 1-3 apply."""
    n = len(first_names)
    keys = [name_key(f, l) for f, l in zip(first_names, last_names)]
    valid_alt = [normalize_alt(a) for a in alts]

    # ---- pass 1: per-alt name clusters ------------------------------------
    by_alt: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(valid_alt):
        if a:
            by_alt[a].append(i)

    review: list[dict] = []
    alt_identity: dict[tuple[str, str], str] = {}   # (alt, name_key) -> archer_id
    junk_alts: set[str] = set()
    for alt, idxs in by_alt.items():
        counts = Counter(keys[i] for i in idxs)
        clusters: list[list[str]] = []
        for k, _ in counts.most_common():      # largest name first anchors a cluster
            for cl in clusters:
                if names_compatible(cl[0], k):
                    cl.append(k)
                    break
            else:
                clusters.append([k])
        if len(clusters) > MAX_NAME_CLUSTERS_PER_ALT or (len(clusters) > 1 and len(alt) <= 4):
            # >4 incompatible names, or any conflict on a 3-4 digit number: an event-local sequence
            # number shared by unrelated people, not a person id.
            junk_alts.add(alt)
            review.append({"kind": "junk_alt", "alt": alt, "names": "; ".join(c[0] for c in clusters[:8]),
                           "detail": f"{len(clusters)} incompatible names share this short/overloaded alt; treated as not-a-person-id"})
            continue
        for ci, cl in enumerate(clusters):
            aid = f"A{alt}" if ci == 0 else f"A{alt}~{ci + 1}"
            for k in cl:
                alt_identity[(alt, k)] = aid
        if len(clusters) > 1:
            review.append({"kind": "alt_name_conflict", "alt": alt, "names": " | ".join(c[0] for c in clusters),
                           "detail": "same alt, incompatible names: split into separate identities"})

    out_id: list[str] = [""] * n
    basis: list[str] = [""] * n
    for i in range(n):
        a = valid_alt[i]
        if a and a not in junk_alts:
            out_id[i] = alt_identity[(a, keys[i])]
            basis[i] = "alt"

    # ---- pass 2: bridge re-issued weak alts into one person ----------------------
    if genders is not None and divisions is not None and tournaments is not None:
        mapping, bridge_review = bridge(out_id, keys, genders, clubs if clubs is not None else [""] * n, divisions, tournaments,
                                        marquee=marquee, years=years)
        for i in range(n):
            tgt = mapping.get(out_id[i]) if out_id[i] else None
            if tgt:
                out_id[i] = tgt
                basis[i] = "alt_bridged"
        review.extend(bridge_review)

    # ---- pass 3: records with no usable alt -------------------------------------
    # Join an existing alt-identity only when this exact name belongs to exactly ONE final
    # identity (computed AFTER bridging, so a re-issued-alt guest is one identity, not 14).
    name_to_ids: dict[str, set[str]] = defaultdict(set)
    for i in range(n):
        if out_id[i]:
            name_to_ids[keys[i]].add(out_id[i])
    for i in range(n):
        if out_id[i]:
            continue
        k = keys[i]
        cands = name_to_ids.get(k, set())
        if len(cands) == 1:
            out_id[i] = next(iter(cands))
            basis[i] = "name_matched_to_alt"
        else:
            out_id[i] = f"N{_hash(k or str(i))}"
            basis[i] = "name_only_ambiguous" if len(cands) > 1 else "name_only"

    # ---- pass 4: attach leftover fragments to the only strong-id archer with the same name -----
    if genders is not None and events is not None and age_classes is not None and years is not None:
        mapping4, nb_review = name_bridge(out_id, keys, genders, events, age_classes, years, marquee=marquee)
        for i in range(n):
            tgt = mapping4.get(out_id[i])
            if tgt:
                out_id[i] = tgt
                basis[i] = "name_bridged"
        review.extend(nb_review)

    # ---- review: one name, several alts that look like the same person ------
    alts_by_name: dict[str, set[str]] = defaultdict(set)
    club_by_alt: dict[str, Counter] = defaultdict(Counter)
    year_by_alt: dict[str, set] = defaultdict(set)
    for i in range(n):
        if basis[i] == "alt":
            alts_by_name[keys[i]].add(out_id[i])
            if clubs is not None and clubs[i]:
                club_by_alt[out_id[i]][str(clubs[i]).strip().lower()] += 1
            if years is not None:
                year_by_alt[out_id[i]].add(years[i])
    for k, ids in alts_by_name.items():
        if len(ids) < 2 or not k:
            continue
        ids = sorted(ids)
        shared_club = set.intersection(*[set(club_by_alt[x]) for x in ids]) if clubs is not None else set()
        if shared_club:
            review.append({"kind": "possible_duplicate_person", "alt": ",".join(ids), "names": k,
                           "detail": f"same name, different alts, shared club(s): {sorted(shared_club)[:3]}"})
    return Resolution(out_id, basis, review)
