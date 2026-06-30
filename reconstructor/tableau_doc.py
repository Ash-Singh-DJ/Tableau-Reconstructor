"""
tableau_doc.py -- format-agnostic IO for Tableau documents (pure stdlib).

A Tableau *workbook* (.twbx) bundles a .twb whose XML root is <workbook>, with a
<datasources> container holding one or more <datasource> elements (plus
<worksheet>s). A Tableau *data source* (.tdsx) bundles a .tds whose XML root IS a
single <datasource> element -- no <workbook> wrapper, no <datasources> container,
and no worksheets. A standalone .tds datasource also carries no name/caption
attribute; it identifies itself with `formatted-name`.

Both formats share the SAME <datasource> internals (federated connection,
named-connections, relations, connection-level metadata-records, calc <column>s,
<extract>), so the swap engines operate on either once they can (a) find the
bundled XML member, (b) enumerate the <datasource> element(s) regardless of root,
(c) preserve the XML prefix when re-serializing, and (d) match/label a datasource
that may only have a formatted-name. This module centralizes exactly those concerns;
everything else in the engines is shared verbatim across the two formats.
"""

import os
import xml.etree.ElementTree as ET
import zipfile

DOC_MEMBER_EXTS = ('.twb', '.tds')      # bundled XML members
ARCHIVE_EXTS = ('.twbx', '.tdsx')       # the zip containers we read/write


def read_doc(path):
    """Return (member_name, raw_text) for the .twb/.tds bundled in a .twbx/.tdsx."""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.endswith(DOC_MEMBER_EXTS)]
        if not names:
            raise RuntimeError(f'No .twb or .tds found inside {path}')
        return names[0], z.read(names[0]).decode('utf-8')


def parse_doc(path):
    """Return the parsed XML root of the bundled .twb/.tds, or None if absent."""
    try:
        _, raw = read_doc(path)
    except RuntimeError:
        return None
    return ET.fromstring(raw)


def split_prefix(raw):
    """Return the text before the root element (XML declaration + build comment),
    so a re-serialized root can be concatenated back without losing them. Handles a
    <workbook>-rooted .twb (workbook comes first) and a <datasource>-rooted .tds."""
    for marker in ('<workbook', '<datasource'):
        i = raw.find(marker)
        if i != -1:
            return raw[:i]
    raise RuntimeError('document has neither a <workbook> nor a <datasource> root')


def datasource_elements(root):
    """Enumerate the <datasource> element(s) regardless of document type: a .tds
    root IS the datasource; a .twb root is <workbook> with a <datasources>
    container."""
    if root.tag == 'datasource':
        return [root]
    dss = root.find('.//datasources')
    return dss.findall('datasource') if dss is not None else []


def ds_label(ds):
    """The human label used for matching/reporting: caption, then federated name,
    then a standalone .tds's formatted-name."""
    return ds.get('caption') or ds.get('name') or ds.get('formatted-name')


def matches(ds, match):
    """True if `match` equals any of the datasource's identifying attributes
    (caption, federated name, or a standalone .tds's formatted-name)."""
    return match in (ds.get('caption'), ds.get('name'), ds.get('formatted-name'))


def default_output_path(input_path, label):
    """`<dir>/<base> - <label><same ext>` next to the input, preserving .twbx/.tdsx."""
    base, ext = os.path.splitext(os.path.basename(input_path))
    return os.path.join(os.path.dirname(os.path.abspath(input_path)),
                        f'{base} - {label}{ext}')
