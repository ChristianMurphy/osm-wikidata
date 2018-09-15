#!/usr/bin/python3
from collections import defaultdict
from unidecode import unidecode
from num2words import num2words
from .utils import remove_start, normalize_url, any_upper

import re

from enum import Enum

re_strip_non_chars = re.compile(r'[^-@\w]', re.U)
re_strip_non_chars_and_dash = re.compile(r'[^@\w]', re.U)
re_non_char_start = re.compile(r'^[^@\w]*', re.U)
re_keep_commas = re.compile(r'[^@\w, ]', re.U)
re_number_start = re.compile('^(?:(?:Number|No)s?\.? )?(\d[-\d]*,? .*$)')
re_uk_postcode_start = re.compile('^[a-z][a-z]\d+[a-z]?$', re.I)

re_ordinal_number = re.compile(r'([0-9]+)(?:st|nd|rd|th)\b', re.I)

re_strip_words = re.compile(r'([ -])(?:the|and|at|of|de|di|le|la|les|von|pw\.)(?=\1)')

MatchType = Enum('Match', ['good', 'wikidata_trimmed', 'both_trimmed', 'trim',
                           'address', 'initials', 'initials_trim'])

abbr = {
    'avenue': 'ave',
    'street': 'st',
    'road': 'rd',
    'boulevard': 'blvd',
    'drive': 'dr',
    'lane': 'ln',
    'square': 'sq',
    'north': 'n',
    'south': 's',
    'east': 'e',
    'west': 'w',
    'northwest': 'nw',
    'northeast': 'ne',
    'southwest': 'sw',
    'southeast': 'se',
}
re_abbr = re.compile(r'\b(' + '|'.join(abbr.keys()) + r')\b', re.I)

re_address_common_end = re.compile('^(.+)(' + '|'.join(abbr.keys()) + '|plaza)$', re.I)

bad_name_fields = {'tiger:name_base', 'name:right',
                   'name:left', 'gnis:county_name', 'openGeoDB:name'}

def no_alpha(s):
    return all(not c.isalpha() for c in s)

class Match(object):
    def __init__(self, match_type):
        self.match_type = match_type
        self.wikidata_name = None
        self.wikidata_source = None
        self.osm_name = None
        self.osm_key = None

def tidy_name(n):
    # expects to be passed a name in lowercase
    n = n.replace('saint ', 'st ')
    n = n.replace(' church of england ', ' ce ')
    n = n.replace(' cofe ', ' ce ')
    n = n.replace(' c of e ', ' ce ')
    n = n.replace(' roman catholic ', ' rc ')
    n = n.replace(' catholic ', ' rc ')
    n = n.replace(' preparatory school', ' prep school')
    n = n.replace(' incorporated', ' inc')
    n = n.replace(' cooperative', ' coop')
    n = n.replace(' co-operative', ' coop')
    if n.startswith('the '):
        n = n[4:]
    elif n.endswith("'s"):
        n = n[:-2]
    if len(n) > 1 and n[-1] == 's':
        n = n[:-1]
    if not n.lstrip().startswith('s '):
        n = n.replace('s ', ' ').replace("s' ", '').replace('s-', '-')
    n = re_strip_words.sub(lambda m: m.group(1), n)
    n = n.replace('center', 'centre').replace('theater', 'theatre')

    decoded = unidecode(n).strip()
    if not any(c.isalnum() for c in decoded):
        return n.strip()
    return decoded

def initials_match(n1, n2, endings=None):
    n1_lc = n1.lower()
    initals = ''.join(term[0] for term in n2.split() if term[0].isalnum()).upper()
    if len(initals) < 3 or len(n1) < 3:
        return
    if initals == n1:
        return Match(MatchType.initials)
    if initals == ''.join(c for c in n1 if c.isalnum()):
        return Match(MatchType.initials)
    if any(initals == trim for trim in
            [n1[:-len(end)].strip() for end in endings or [] if n1_lc.endswith(end.lower())]):
        return Match(MatchType.initials_trim)

def match_with_words_removed(osm, wd, words):
    if not words:
        return
    wd_char_only = re_strip_non_chars.sub('', wd)
    osm_char_only = re_strip_non_chars.sub('', osm)
    words = [re_strip_non_chars.sub('', w) for w in words]
    osm_versions = {osm_char_only.replace(word, '')
                    for word in words} | {osm_char_only}
    wd_versions = {wd_char_only.replace(word, '')
                    for word in words} | {wd_char_only}

    best_match = None
    for osm_filtered in osm_versions:
        if not osm_filtered:
            continue
        for wd_filtered in wd_versions:
            if not wd_filtered or osm_filtered != wd_filtered:
                continue
            if wd_filtered == wd_char_only:
                return Match(MatchType.good)
            match_type = (MatchType.both_trimmed
                          if osm_filtered != osm_char_only
                          else MatchType.wikidata_trimmed)
            best_match = Match(match_type)
    return best_match

def strip_non_chars_match(osm, wd, dash_okay=True):
    pattern = re_strip_non_chars if dash_okay else re_strip_non_chars_and_dash

    wc_stripped = pattern.sub('', wd)
    osm_stripped = pattern.sub('', osm)

    return wc_stripped and osm_stripped and wc_stripped == osm_stripped

def prefix_name_match(osm, wd):
    wd_lc = wd.lower()
    osm_lc = osm.lower()

    if osm_lc.startswith(wd_lc):
        return osm[len(wd):].strip()

    space = osm.find(' ')
    while space != -1:
        osm_start = osm_lc[:space]
        if strip_non_chars_match(osm_start, wd_lc):
            return osm[space:].strip()
        space = osm.find(' ', space + 1)

def check_for_intials_match(initials, name):
    if any(c.islower() for c in initials):
        return
    if len([c for c in initials if c.isupper()]) < 2:
        return
    x = bool(initials_match(initials, name))
    return x

def strip_non_char_start(s):
    return re_non_char_start.sub('', s)

def drop_initials(name):
    first_space = name.find(' ')
    if first_space == -1:
        return
    tail = strip_non_char_start(name[first_space:])

    if check_for_intials_match(name[:first_space], tail):
        return tail

    last_space = name.rfind(' ')
    if last_space == first_space:
        return
    head = strip_non_char_start(name[:last_space])
    if check_for_intials_match(name[last_space:], head):
        return head

def split_on_upper(name):
    upper_positions = [num for num, char in enumerate(name) if char.isupper()]

    xpos = 0
    for pos in upper_positions:
        text = name[xpos:pos].rstrip()
        if text:
            yield text
        xpos = pos
    text = name[xpos:].rstrip()
    if text:
        yield text

def split_on_upper_and_tidy(name):
    parts = [re_strip_non_chars.sub('', part) for part in split_on_upper(name)]
    return [part for part in parts if part]

def name_containing_initials(n1, n2):
    if not any_upper(n1) or not any_upper(n2):
        return False
    n1_split = split_on_upper_and_tidy(n1)
    n2_split = split_on_upper_and_tidy(n2)

    if len(n1_split) != len(n2_split) or len(n1_split) < 3:
        return False

    for part1, part2 in zip(n1_split, n2_split):
        if part1 == part2:
            continue
        if len(part1) == 1 and part2[0] == part1:
            continue
        if len(part2) == 1 and part1[0] == part2:
            continue
        return False
    return True

def plural_word_name_in_other_name(n1, n2):
    return (' ' not in n1 and
            ' ' in n2 and n1.endswith('s') and
            n1 not in n2 and
            n1[:-1] in n2)

def name_match_main(osm, wd, endings=None, debug=False):
    if not wd or not osm:
        return

    if wd == osm:
        return Match(MatchType.good)

    if name_containing_initials(osm, wd):
        return Match(MatchType.good)

    osm_lc = osm.lower()
    wd_lc = wd.lower()

    if set(osm_lc.split()) == set(wd_lc.split()):
        return Match(MatchType.good)

    if strip_non_chars_match(osm_lc, wd_lc, dash_okay=False):
        return Match(MatchType.good)

    m = initials_match(osm, wd, endings) or initials_match(wd, osm, endings)
    if m:
        return m

    if strip_non_chars_match(osm_lc, wd_lc):
        return Match(MatchType.good)

    wd_tidy = tidy_name(wd_lc)
    osm_tidy = tidy_name(osm_lc)

    if not wd_tidy or not osm_tidy:
        return

    if wd_tidy == osm_tidy:
        return Match(MatchType.good)

    endings = set(endings or []) | {'house'}
    m = match_with_words_removed(osm_lc, wd_lc, endings)
    if m:
        if 'church' in osm_lc and 'church' in wd_lc:
            m.match_type = MatchType.good
        return m

    plural_in_other_name = (plural_word_name_in_other_name(osm_lc, wd_lc) or
                            plural_word_name_in_other_name(wd_lc, osm_lc))

    if endings:
        tidy_endings = [tidy_name(e) for e in endings]
        m = match_with_words_removed(osm_tidy, wd_tidy, tidy_endings)
        if m and not plural_in_other_name:
            return m

    if strip_non_chars_match(osm_tidy, wd_tidy, dash_okay=False):
        return Match(MatchType.good)

    if 'washington, d' in wd_tidy:  # special case for Washington, D.C.
        wd_tidy = wd_tidy.replace('washington, d', 'washington d')
    comma = wd_tidy.rfind(', ')
    if comma != -1 and not osm_tidy.isdigit():
        wc_part1 = wd_tidy[:comma]
        if wc_part1 == osm_tidy or strip_non_chars_match(osm_tidy, wc_part1):
            return Match(MatchType.good)

    if wd_tidy.split() == list(reversed(osm_tidy.split())):
        return Match(MatchType.good)

    wd_tidy = re_keep_commas.sub('', wd_tidy)
    osm_tidy = re_keep_commas.sub('', osm_tidy)

    comma = wd_tidy.rfind(', ')
    if comma != -1 and not osm_tidy.isdigit():
        if wd_tidy[:comma] == osm_tidy:
            return Match(MatchType.good)
        if remove_start(wd_tidy[:comma], 'the ') == remove_start(osm_tidy, 'the '):
            return Match(MatchType.good)

    wd_tidy = re_strip_non_chars.sub('', wd_tidy)
    osm_tidy = re_strip_non_chars.sub('', osm_tidy)
    if wd_tidy == osm_tidy:
        return Match(MatchType.good)

    if wd_tidy.startswith('the'):
        wd_tidy = wd_tidy[3:]
    if osm_tidy.startswith('the'):
        osm_tidy = osm_tidy[3:]
    if wd_tidy == osm_tidy:
        return Match(MatchType.good)

    if plural_in_other_name:
        return

    for end in ['building', 'complex', 'house'] + list(endings or []):
        if wd_tidy.endswith(end) and wd_tidy[:-len(end)] == osm_tidy:
            return Match(MatchType.trim)
        if wd_tidy.startswith(end) and wd_tidy[len(end):] == osm_tidy:
            return Match(MatchType.trim)
        if osm_tidy.endswith(end) and osm_tidy[:-len(end)] == wd_tidy:
            return Match(MatchType.trim)
        if osm_tidy.startswith(end) and osm_tidy[len(end):] == wd_tidy:
            return Match(MatchType.trim)
    return

def strip_place_name(name, place_name):
    for word in 'of', 'de', 'di', 'at':
        search = f' {word} {place_name}'
        if search in name:
            return name.replace(search, '')
    return name.replace(place_name, '')

def more_place_name_varients(place_names):
    place_names = set(place_names)
    for n in set(place_names):
        for e in 'city', 'county':
            if n.lower().endswith(' ' + e) and len(n) > len(e) + 1:
                place_names.add(n[:-(len(e) + 1)])
    return place_names

def name_match(osm, wd, endings=None, debug=False, place_names=None):
    match = name_match_main(osm, wd, endings, debug)
    if match:
        return match

    osm_no_intitals = drop_initials(osm)
    if osm_no_intitals:
        match = name_match_main(osm_no_intitals, wd, endings, debug)
        if match:
            return match

    for start in 'Tomb of ', 'Statue of ', 'Memorial to ':
        if wd.startswith(start) and name_match_main(osm, wd[len(start):], endings):
            return Match(MatchType.trim)

    start = 'site of'
    if osm.lower().startswith(start):
        if name_match_main(osm[len(start):], wd, endings):
            return Match(MatchType.trim)

    end = ' And Attached Railings'.lower()
    if wd.lower().endswith(end) and name_match_main(osm, wd[:-len(end)], endings):
        return Match(MatchType.trim)

    if place_names:
        for place_name in more_place_name_varients(place_names):
            if not (place_name in osm or place_name in wd):
                continue
            match = name_match_main(strip_place_name(osm, place_name),
                                    strip_place_name(wd, place_name),
                                    endings, debug)
            if match:
                return match

    if ';' not in osm:
        return
    for osm_name in osm.split(';'):
        match = name_match(osm_name.strip(), wd, endings=endings, debug=debug,
                           place_names=place_names)
        if match:
            return match

def normalize_name(name):
    name = re_ordinal_number.sub(lambda m: num2words(int(m.group(1)), to='ordinal'), name)
    return re_strip_non_chars.sub('', name.lower())

def has_address(osm_tags):
    return any('addr:' + part in osm_tags for part in ('housenumber', 'full'))

def any_url_match(osm_value, values):
    osm_url = normalize_url(osm_value)
    return any(osm_url == normalize_url(wd_url) for wd_url in values)

def check_identifier(osm_tags, item_identifiers):
    if not item_identifiers:
        return False
    for k, v in item_identifiers.items():
        for values, label in v:
            values = set(values) | {i.replace(' ', '') for i in values if ' ' in i}
            osm_value = osm_tags.get(k)
            if not osm_value:
                continue
            if osm_value in values:
                return True
            if ' ' in osm_value and osm_value.replace(' ', '') in values:
                return True
            if label == 'website' and any_url_match(osm_value, values):
                return True
    return False

def check_for_address_in_extract(osm_tags, extract):
    if not extract or not has_address(osm_tags):
        return

    def address_in_extract(address):
        address = re_abbr.sub(lambda m: '(' + m.group(1) + '|' + abbr[m.group(1).lower()] + '\.?)', re.escape(address))
        # address = re_directions.sub(lambda m: '(' + m.group(1) + '|' + m.group(1)[0] + ')', address)

        return bool(re.search(r'\b' + address, extract, re.I))

    if 'addr:housenumber' in osm_tags and 'addr:street' in osm_tags:
        address = osm_tags['addr:housenumber'] + ' ' + osm_tags['addr:street']
        if address_in_extract(address):
            return True

    if 'addr:full' in osm_tags and address_in_extract(osm_tags['addr:full']):
        return True

def check_name_matches_address(osm_tags, wikidata_names):
    if not has_address(osm_tags):
        return
    # if 'addr:housenumber' not in osm_tags or 'addr:street' not in osm_tags:
    #     return
    number_start_iter = (re_number_start.match(name)
                         for name in wikidata_names
                         if not name.lower().endswith(' building'))
    number_start = {m.group(1) for m in number_start_iter if m}
    if not number_start:
        return

    strip_comma = [name[:name.rfind(',')]
                   for name in set(number_start)
                   if ',' in name]
    number_start.update(n for n in strip_comma if not n.isdigit())
    norm_number_start = {normalize_name(name) for name in number_start}

    postcode = osm_tags.get('addr:postcode')
    if postcode:
        postcode = postcode.lower()

    if 'addr:housenumber' in osm_tags and 'addr:street' in osm_tags:
        osm_address = osm_tags['addr:housenumber'] + ' ' + osm_tags['addr:street']
        norm_osm_address = normalize_name(osm_address)
        if any(name == norm_osm_address for name in norm_number_start):
            return True

        for i in number_start:
            name, _, postcode_start = i.rpartition(' ')

            if postcode and not postcode.startswith(postcode_start.lower()):
                continue

            if (re_uk_postcode_start.match(postcode_start) and
                    normalize_name(name) == norm_osm_address):
                return True

        if any(name.startswith(norm_osm_address) or norm_osm_address.startswith(name)
               for name in norm_number_start):
            return  # not sure

        m = re_address_common_end.match(norm_osm_address)
        if m:
            short = m.group(1)
            if any(name.startswith(short) for name in norm_number_start):
                return

    if 'addr:full' in osm_tags:
        osm_address = normalize_name(osm_tags['addr:full'])
        if any(osm_address.startswith(name) for name in norm_number_start):
            return True

        for i in number_start:
            name, _, postcode_start = i.rpartition(' ')

            if (re_uk_postcode_start.match(postcode_start) and
                    normalize_name(name) == osm_address):
                return True

    # if we find a name from wikidata matches the OSM name we can be more relaxed
    # about the address
    name_match = 'name' in osm_tags and any(n == osm_tags['name'] for n in number_start)

    return None if name_match else False

def get_names(osm_tags):
    return {k: v for k, v in osm_tags.items()
             if ('name' in k and k not in bad_name_fields) or k == 'operator'}

def intials_matches_other_wikidata_name(initials, wikidata_names):
    return any(w != initials and initials_match(initials, w)
               for w in wikidata_names.keys())

def check_for_match(osm_tags, wikidata_names, endings=None, place_names=None):
    names = get_names(osm_tags)
    operator = names['operator'].lower() if 'operator' in names else None
    if not names or not wikidata_names:
        return {}

    if 'addr:city' in osm_tags:
        city = osm_tags['addr:city'].lower()
        endings = set(endings or [])
        endings |= {
            city,
            'in ' + city,  # English / German / Dutch
            'w ' + city,   # Polish
            'à ' + city,   # French
            'en ' + city,  # Spanish
            'em ' + city,  # Portuguese
            'v ' + city,   # Czech
            'i ' + city,   # Danish / Norwegian / Swedish
            'a ' + city,   # Italian
        }

    name = defaultdict(list)
    cache = {}
    for w, source in wikidata_names.items():
        for osm_key, o in names.items():
            if (o, w) in cache:
                result = cache[(o, w)]
                if not result:
                    continue
            else:
                m = name_match(o, w, endings, place_names=place_names)
                if not m and operator and o.lower().startswith(operator):
                    m = name_match(o[len(operator):].rstrip(), w, endings,
                                   place_names=place_names)
                    if m and m.match_type in (MatchType.both_trimmed,
                                              MatchType.wikidata_trimmed):
                        continue
                if not m:
                    cache[(o, w)] = None
                    continue
                # if we had to trim both names and the OSM name is from the
                # operator tag it doesn't count
                if (m.match_type == MatchType.both_trimmed and
                        osm_key == 'operator'):
                    continue
                result = (m.match_type.name, w, source)
            if (result[0] == 'initials' and
                    intials_matches_other_wikidata_name(w, wikidata_names)):
                continue
            name[osm_key].append(result)
    if name:
        return dict(name)

    for w, source in wikidata_names.items():
        for osm_key, o in names.items():
            left_over = prefix_name_match(o, w)
            if not left_over:
                continue
            for second_w, second_source in wikidata_names.items():
                if second_w == w:
                    continue
                m = name_match(left_over, second_w, place_names=place_names)
                if not m:
                    continue
                name[osm_key].append(('prefix', w, source))
                break

    return dict(name)

def get_all_matches(osm_tags, wikidata_names, endings=None):
    names = get_names(osm_tags)

    matches = []
    for w, source in wikidata_names.items():
        for osm_key, o in names.items():
            m = name_match(o, w, endings)
            if m:
                m.wikidata_name = w
                m.wikidata_source = source
                m.osm_name = o
                m.osm_key = osm_key
                matches.append(m)

    # FIXME this code is broken
    # address_match = check_name_matches_address(osm_tags, wikidata_names)
    # if address_match:
    #     m = address_match
    #     m.wikidata_name = w
    #     m.wikidata_source = source
    #     m.osm_name = o
    #     m.osm_key = osm_key
    #     matches.append(m)
    return matches
