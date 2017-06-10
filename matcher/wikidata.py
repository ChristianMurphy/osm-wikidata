from flask import render_template_string
from urllib.parse import unquote
from collections import defaultdict
from .utils import chunk, drop_start
from .mail import error_mail
import requests
from . import user_agent_headers

page_size = 50
wd_entity = 'http://www.wikidata.org/entity/Q'
enwiki = 'https://en.wikipedia.org/wiki/'
skip_tags = {'route:road',
             'route=road',
             'highway=primary',
             'highway=road',
             'highway=service',
             'highway=motorway',
             'highway=trunk',
             'highway=unclassified',
             'highway',
             'landuse'
             'name',
             'website',
             'addr:street',
             'type=associatedStreet',
             'type=waterway',
             'waterway=river'}

# search for items in bounding box that have an English Wikipedia article
wikidata_enwiki_query = '''
SELECT ?place ?placeLabel (SAMPLE(?location) AS ?location) ?article WHERE {
    SERVICE wikibase:box {
        ?place wdt:P625 ?location .
        bd:serviceParam wikibase:cornerWest "Point({{ west }} {{ south }})"^^geo:wktLiteral .
        bd:serviceParam wikibase:cornerEast "Point({{ east }} {{ north }})"^^geo:wktLiteral .
    }
    ?article schema:about ?place .
    ?article schema:inLanguage "en" .
    ?article schema:isPartOf <https://en.wikipedia.org/> .
    SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
GROUP BY ?place ?placeLabel ?article
'''

wikidata_point_query = '''
SELECT ?place (SAMPLE(?location) AS ?location) ?article WHERE {
    SERVICE wikibase:around {
        ?place wdt:P625 ?location .
        bd:serviceParam wikibase:center "Point({{ lon }} {{ lat }})"^^geo:wktLiteral .
        bd:serviceParam wikibase:radius "{{ '{:.1f}'.format(radius) }}" .
    }
    ?article schema:about ?place .
    ?article schema:inLanguage "en" .
    ?article schema:isPartOf <https://en.wikipedia.org/> .
}
GROUP BY ?place ?article
'''

wikidata_subclass_osm_tags = '''
SELECT DISTINCT ?item ?itemLabel ?tag
WHERE
{
  {
    wd:{{qid}} wdt:P31/wdt:P279* ?item .
    {
        ?item wdt:P1282 ?tag .
    }
    UNION
    {
        ?item wdt:P641 ?sport .
        ?sport wdt:P1282 ?tag
    }
    UNION
    {
        ?item wdt:P140 ?religion .
        ?religion wdt:P1282 ?tag
    }
  }
  UNION
  {
      wd:{{qid}} wdt:P1435 ?item .
      ?item wdt:P1282 ?tag
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}'''

# search for items in bounding box that have OSM tags in the subclass tree
wikidata_item_tags = '''
SELECT ?place ?placeLabel (SAMPLE(?location) AS ?location) ?address ?street ?item ?itemLabel ?tag WHERE {
    SERVICE wikibase:box {
        ?place wdt:P625 ?location .
        bd:serviceParam wikibase:cornerWest "Point({{ west }} {{ south }})"^^geo:wktLiteral .
        bd:serviceParam wikibase:cornerEast "Point({{ east }} {{ north }})"^^geo:wktLiteral .
    }
    ?place wdt:P31/wdt:P279* ?item .
    ?item wdt:P1282 ?tag .
    OPTIONAL { ?place wdt:P969 ?address } .
    OPTIONAL { ?place wdt:P669 ?street } .
    SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
GROUP BY ?place ?placeLabel ?address ?street ?item ?itemLabel ?tag
'''

class QueryError(Exception):
    pass

def get_query(q, south, north, west, east):
    return render_template_string(q,
                                  south=south,
                                  north=north,
                                  west=west,
                                  east=east)

def get_enwiki_query(*args):
    return get_query(wikidata_enwiki_query, *args)

def get_item_tag_query(*args):
    return get_query(wikidata_item_tags, *args)

def get_point_query(lat, lon, radius):
    return render_template_string(wikidata_point_query,
                                  lat=lat,
                                  lon=lon,
                                  radius=float(radius) / 1000.0)

def run_query(query):
    url = 'https://query.wikidata.org/bigdata/namespace/wdq/sparql'
    r = requests.get(url,
                     params={'query': query, 'format': 'json'},
                     headers=user_agent_headers())
    if r.status_code == 500:
        error_mail('wikidata query error', query, r)
        raise QueryError
    assert r.status_code == 200
    return r.json()['results']['bindings']

def wd_uri_to_id(value):
    return int(drop_start(value, wd_entity))

def wd_uri_to_qid(value):
    assert value.startswith(wd_entity)
    return value[len(wd_entity)-1:]

def parse_enwiki_query_old(query):
    return [{
        'location': i['location']['value'],
        'id': wd_uri_to_id(i['place']['value']),
        'enwiki': unquote(drop_start(i['article']['value'], enwiki)),
    } for i in query]

def parse_enwiki_query(rows):
    return {wd_uri_to_qid(row['place']['value']):
            {
                'query_label': row['placeLabel']['value'],
                'enwiki': unquote(drop_start(row['article']['value'], enwiki)),
                'location': row['location']['value'],
                'tags': set(),
            } for row in rows}

def drop_tag_prefix(v):
    if v.startswith('Key:') and '=' not in v:
        return v[4:]
    if v.startswith('Tag:') and '=' in v:
        return v[4:]

def parse_item_tag_query(rows, items):
    for row in rows:
        tag_or_key = drop_tag_prefix(row['tag']['value'])
        if not tag_or_key or tag_or_key in skip_tags:
            continue
        qid = wd_uri_to_qid(row['place']['value'])

        if qid not in items:
            items[qid] = {
                'query_label': row['placeLabel']['value'],
                'location': row['location']['value'],
                'tags': set(),
            }
            for k in 'address', 'street':
                if k in row:
                    items[qid][k] = row[k]['value']
        items[qid]['tags'].add(tag_or_key)

def entity_iter(ids):
    wikidata_url = 'https://www.wikidata.org/w/api.php'
    params = {
        'format': 'json',
        'formatversion': 2,
        'action': 'wbgetentities',
    }
    for cur in chunk(ids, page_size):
        params['ids'] = '|'.join(cur)
        json_data = requests.get(wikidata_url,
                                 params=params,
                                 headers=user_agent_headers()).json()
        for qid, entity in json_data['entities'].items():
            yield qid, entity

def get_entity(qid):
    wikidata_url = 'https://www.wikidata.org/w/api.php'
    params = {
        'format': 'json',
        'formatversion': 2,
        'action': 'wbgetentities',
        'ids': qid,
    }
    json_data = requests.get(wikidata_url,
                             params=params,
                             headers=user_agent_headers()).json()
    try:
        return list(json_data['entities'].values())[0]
    except KeyError:
        return None

def get_entities(ids):
    if not ids:
        return []
    wikidata_url = 'https://www.wikidata.org/w/api.php'
    params = {
        'format': 'json',
        'formatversion': 2,
        'action': 'wbgetentities',
        'ids': '|'.join(ids),
    }
    json_data = requests.get(wikidata_url,
                             params=params,
                             headers=user_agent_headers()).json()
    return list(json_data['entities'].values())

def names_from_entity(entity, skip_lang={'ar', 'arc', 'pl'}):
    if not entity:
        return

    ret = defaultdict(list)
    cat_start = 'Category:'

    for k, v in entity['labels'].items():
        if k in skip_lang:
            continue
        ret[v['value']].append(('label', k))

    for k, v in entity['sitelinks'].items():
        if k + 'wiki' in skip_lang:
            continue
        title = v['title']
        if title.startswith(cat_start):
            title = title[len(cat_start):]

        ret[title].append(('sitelink', k))

    for lang, value_list in entity.get('aliases', {}).items():
        if lang in skip_lang or len(value_list) > 3:
            continue
        for name in value_list:
            ret[name['value']].append(('alias', lang))

    return ret

def osm_key_query(qid):
    return render_template_string(wikidata_subclass_osm_tags, qid=qid)

def get_osm_keys(query):
    r = requests.get('https://query.wikidata.org/bigdata/namespace/wdq/sparql',
                     params={'query': query, 'format': 'json'},
                     headers=user_agent_headers())
    assert r.status_code == 200
    return r.json()['results']['bindings']

def parse_osm_keys(rows):
    start = 'http://www.wikidata.org/entity/'
    items = {}
    for row in rows:
        uri = row['item']['value']
        qid = drop_start(uri, start)
        tag = row['tag']['value']
        for i in 'Key:', 'Tag':
            if tag.startswith(i):
                tag = tag[4:]
        if qid not in items:
            items[qid] = {
                'uri': uri,
                'label': row['itemLabel']['value'],
                'tags': set(),
            }
        items[qid]['tags'].add(tag)
    return items
