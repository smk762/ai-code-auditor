"""brand._safe: processing instructions are refused, double-quoted local url() references pass."""
from __future__ import annotations

import pytest

from auditor import brand

NS = b'xmlns="http://www.w3.org/2000/svg"'


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml-stylesheet type="text/xsl" href="https://evil.test/x.xsl"?><svg ' + NS + b"/>",
        b'<?xml version="1.0"?><?xml-stylesheet type="text/css" href="https://evil.test/x.css"?><svg ' + NS + b"/>",
        b"<svg " + NS + b'><?xml-stylesheet href="https://evil.test/x.css"?><rect width="1"/></svg>',
        b"<svg " + NS + b'><rect fill="url(\'&quot;https://evil.test/x)"/></svg>',
        b"<svg " + NS + b"><rect fill='url(\"https://evil.test/#x\")'/></svg>",
    ],
)
def test_processing_instructions_and_external_urls_are_refused(payload):
    assert not brand._safe(payload)


@pytest.mark.parametrize("ref", [b"url(#g)", b"url('#g')", b'url("#g")', b'url( " #g " )'])
def test_local_fragment_references_pass_in_every_quoting(ref):
    svg = (
        b"<svg " + NS + b'><defs><linearGradient id="g"><stop offset="0"/></linearGradient></defs>'
        b'<rect width="1" fill="' + ref.replace(b'"', b"&quot;") + b'"/></svg>'
    )
    assert brand._safe(svg)


def test_a_leading_xml_declaration_is_still_allowed():
    assert brand._safe(b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\n<svg ' + NS + b"/>")
