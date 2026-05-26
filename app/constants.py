CITY_SUFFIXES: list[str] = [
    " Greensboro", " Winston-Salem", " Winston Salem",
    " High Point", " Charlotte", " Salisbury", " Asheville",
    " Raleigh", " Fayetteville", " Wilmington", " Wilson",
]


def strip_city_suffix(kw: str) -> str:
    for suffix in CITY_SUFFIXES:
        if kw.endswith(suffix):
            return kw[:-len(suffix)]
    return kw


MARKET_TO_DISTRICT: dict[str, str] = {
    # MDNC — Middle District of North Carolina
    "greensboro":    "MDNC",
    "winston_salem": "MDNC",
    "high_point":    "MDNC",
    "salisbury":     "MDNC",
    "durham":        "MDNC",
    "concord":       "MDNC",
    "graham":        "MDNC",
    "carthage":      "MDNC",
    "asheboro":      "MDNC",
    # WDNC — Western District of North Carolina
    "charlotte":        "WDNC",
    "asheville":        "WDNC",
    "waynesville":      "WDNC",
    "statesville":      "WDNC",
    "mooresville":      "WDNC",
    "elkin":            "WDNC",
    "north_wilkesboro": "WDNC",
    "morganton":        "WDNC",
    # EDNC — Eastern District of North Carolina
    "ednc":         "EDNC",
    "raleigh":      "EDNC",
    "fayetteville": "EDNC",
    "wilson":       "EDNC",
    "wilmington":   "EDNC",
}
