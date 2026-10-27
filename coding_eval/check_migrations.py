"""Audited corrections to exact legacy check sources; never execute task code."""

import hashlib

# C11's original checks index snapshot fields before asserting their contract.
# A malformed candidate return therefore raises KeyError in a trusted frame.
LEGACY_C11_SHA256 = "15836d688c22b2102140b012a5c494d432bfc11c5b98ba79b0f2b42dcb8633f0"
_START = "def check_state_integrity():\n"
_END = "\n@_case\ndef check_contract():"
_GUARD = """def checked_geometry(value, horizontal=False):
    assert isinstance(value, dict), 'geometry result must be a dict'
    assert {'size', 'move', 'style'} <= set(value), 'geometry result missing required fields'
    assert isinstance(value['style'], dict), 'geometry style must be a dict'
    assert ('width' if horizontal else 'height') in value['style'], 'geometry style missing axis'
    return value

"""


def migrate_check_source(source: str) -> tuple[str, dict | None]:
    """Only this exact check version changes; genuine judge errors stay errors."""
    original_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if original_hash != LEGACY_C11_SHA256:
        return source, None
    start = source.index(_START) + len(_START)
    end = source.index(_END, start)
    body = source[start:end].replace("a.snapshot()", "checked_geometry(a.snapshot())")
    effective = _GUARD + source[:start] + body + source[end:]
    # The reference scroll/update methods call snapshot too. Guard all return
    # values the original checks index, including horizontal style in contract.
    for expression in (
        "s.scroll(100)", "s.update(100,500)", "s.update(100,120)",
        "s.update(600,120)", "solution.geometry(v,c,10)",
        "solution.geometry(3,7,2)",
    ):
        effective = effective.replace(expression, f"checked_geometry({expression})")
    effective = effective.replace(
        "result = solution.geometry(200,500,offset,horizontal)",
        "result = checked_geometry(solution.geometry(200,500,offset,horizontal), horizontal)",
    )
    effective = effective.replace(
        "result = solution.Scrollbar(200,500,horizontal=True).scroll(offset=100)",
        "result = checked_geometry(solution.Scrollbar(200,500,horizontal=True).scroll(offset=100), True)",
    )
    return effective, {
        "migration": "c11-snapshot-contract-v1",
        "original_sha256": original_hash,
        "effective_sha256": hashlib.sha256(effective.encode("utf-8")).hexdigest(),
    }
