"""Decode native observer XML fields into their original Java strings.

The wrapper stores values as Base64 of UTF-16BE code units because generated
SQL may contain NUL or unpaired surrogates forbidden by XML. These are raw observations, without EQ/NEQ
labels. Use read_record(path) for integration; the CLI prints ordinary JSON.
"""
from pathlib import Path
import argparse
import base64
import json
import xml.etree.ElementTree as ET


def read_record(path):
    root = ET.parse(path).getroot()
    if root.tag != 'properties':
        raise ValueError('expected JDK Properties XML')
    values = {}
    for item in root.findall('entry'):
        key = item.attrib['key']
        if key in values:
            raise ValueError('duplicate observation field: ' + key)
        values[key] = item.text or ''
    if values.pop('_encoding', None) != 'base64-utf16be-codeunits':
        raise ValueError('expected base64-utf16be-codeunits observation values')
    return {key: base64.b64decode(value, validate=True).decode('utf-16-be', errors='surrogatepass')
            for key, value in values.items()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('records', nargs='+', type=Path)
    args = parser.parse_args()
    for record in args.records:
        print(json.dumps({'path': str(record), 'record': read_record(record)},
                         ensure_ascii=True, sort_keys=True))
