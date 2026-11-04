"""Nickname / variant dictionary for person names (power-up P6).

Data, not logic: canonical given name -> known variants, covering the
English and Spanish naming pools our customers' lists actually carry.
Lookup is accent-insensitive (José == Jose) and case-insensitive.

SAFETY (the design's founding rule, restated): a nickname match is a
*booster*, never evidence on its own. linkage.pair_evidence applies it
only when an independent contact signal already agrees AND the rest of
the name (surname) is compatible — Robert Garcia vs Bob Garcia with
the same email is one person spelled two ways; Robert Smith vs Bob
Jones is two people, whatever this table says.
"""
import unicodedata

_GROUPS = {
    # ------------------------------------------------ English
    "robert": ("bob", "bobby", "rob", "bert", "robbie"),
    "william": ("bill", "billy", "will", "willie", "liam"),
    "richard": ("rick", "dick", "rich", "ricky", "rickie"),
    "james": ("jim", "jimmy", "jamie"),
    "john": ("jack", "johnny", "jon", "johnnie"),
    "joseph": ("joe", "joey"),
    "michael": ("mike", "mick", "mickey", "mickel"),
    "david": ("dave", "davey"),
    "daniel": ("dan", "danny"),
    "thomas": ("tom", "tommy"),
    "charles": ("chuck", "charlie", "chas", "charley"),
    "edward": ("ed", "eddie", "ted", "ned", "eddy"),
    "elizabeth": ("liz", "beth", "betty", "eliza", "lisa", "bess",
                  "lizzie", "bethany"),
    "margaret": ("maggie", "peggy", "meg", "marge", "margie"),
    "katherine": ("cathy", "kate", "katie", "kathy", "kitty", "catherine",
                  "kathryn", "kaitlyn"),
    "jennifer": ("jen", "jenny", "jenn"),
    "patricia": ("pat", "patty", "tricia"),
    "susan": ("sue", "suzy", "susie"),
    "jessica": ("jess", "jessie"),
    "dorothy": ("dot", "dottie"),
    "anthony": ("tony",),
    "stephen": ("steve", "steven", "steph"),
    "andrew": ("andy", "drew"),
    "joshua": ("josh",),
    "kenneth": ("ken", "kenny"),
    "ronald": ("ron", "ronnie"),
    "donald": ("don", "donnie"),
    "rebecca": ("becky",),
    "deborah": ("deb", "debbie", "debby"),
    "christopher": ("chris", "topher", "christoph"),
    "matthew": ("matt",),
    "jason": ("jay",),
    "jeffrey": ("jeff",),
    "nicholas": ("nick", "nicky", "nico"),
    "jonathan": ("jon", "nathan", "johnny"),
    "samuel": ("sam", "sammy"),
    "benjamin": ("ben", "benny", "benji"),
    "alexander": ("alex", "al", "xander"),
    "frederick": ("fred", "freddie", "freddy"),
    "walter": ("walt", "wally"),
    "harold": ("harry", "hal"),
    "albert": ("al", "bert", "bertie"),
    "arthur": ("art", "artie"),
    "raymond": ("ray",),
    "eugene": ("gene",),
    "russell": ("russ",),
    "virginia": ("ginny", "ginger"),
    "victoria": ("vicky", "vicki", "tori"),
    "cynthia": ("cindy",),
    "kathleen": ("kathy", "kath"),
    "pamela": ("pam",),
    "sandra": ("sandy",),
    "barbara": ("barb", "barbie"),
    "kimberly": ("kim",),
    "michelle": ("shelly", "chelle"),
    "amanda": ("mandy",),
    "melissa": ("mel", "missy", "lissa"),
    "stephanie": ("steph", "stephie"),
    "timothy": ("tim", "timmy"),
    "gerald": ("jerry", "gerry"),
    "lawrence": ("larry", "law"),
    "douglas": ("doug",),
    "zachary": ("zach", "zack", "zac"),
    # ------------------------------------------------ Spanish
    "jose": ("pepe",),
    "francisco": ("paco", "pancho", "curro", "fran"),
    "francisca": ("paquita", "paqui", "fran"),
    "guadalupe": ("lupe", "lupita"),
    "jesus": ("chuy", "chucho"),
    "manuel": ("manolo",),
    "antonio": ("tono", "toni"),
    "dolores": ("lola",),
    "mercedes": ("meche",),
    "ignacio": ("nacho",),
    "guillermo": ("memo",),
    "enrique": ("quique",),
    "roberto": ("beto",),
    "alberto": ("beto", "al"),
    "eduardo": ("lalo",),
    "alejandro": ("alex", "ale"),
    "fernando": ("nando", "fer"),
    "fernanda": ("fer",),
    "josefina": ("pepa", "fina"),
    "margarita": ("marga",),
    "rosario": ("charo",),
    "concepcion": ("concha",),
    "salvador": ("salva",),
    "beatriz": ("bea",),
    "cristina": ("cris",),
    "santiago": ("santi", "tiago"),
    "sebastian": ("seba",),
    "rafael": ("rafa",),
    "miguel": ("migue", "mikel"),
    "andres": ("andresito",),
    "valentina": ("vale", "valen"),
    "isabel": ("isa", "bel"),
    "carmen": ("carmita",),
    "juan": ("juanito", "juani"),
    "pedro": ("perico",),
    "vicente": ("vicen",),
    "emilio": ("milo",),
    "joaquin": ("joaco",),
    "diego": ("dieguito",),
}


def _fold(token):
    """Accent/case folding for lookup: 'José' -> 'jose'."""
    s = unicodedata.normalize("NFKD", str(token or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip(".,'")


_LOOKUP = {}
for _canon, _variants in _GROUPS.items():
    _LOOKUP[_canon] = _canon
    for _v in _variants:
        _LOOKUP[_v] = _canon


def canonical(token):
    """Canonical form of a nickname/variant token, or None when the
    token is not in the dictionary."""
    return _LOOKUP.get(_fold(token))


def are_variants(a, b):
    """True when a and b are DIFFERENT spellings of one given name."""
    fa, fb = _fold(a), _fold(b)
    if not fa or fa == fb:
        return False
    ca, cb = _LOOKUP.get(fa), _LOOKUP.get(fb)
    return bool(ca) and ca == cb


def variant_pairs(tokens_a, tokens_b):
    """(a, b) token pairs across two token lists that are nickname
    variants of each other — used by linkage to detect the pattern."""
    out = []
    for ta in tokens_a:
        for tb in tokens_b:
            if are_variants(ta, tb):
                out.append((ta, tb))
    return out
