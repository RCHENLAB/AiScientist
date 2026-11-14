"""The report validates annotation claims against its bundled curated reference."""
from aiscientist.tools import api

def test_retina_is_a_curated_reference():
    assert "retina" in api.available_references()
    reference = api.load_marker_reference("retina")
    assert reference and reference.get("species", {}).get("human", {}).get("Rod photoreceptor", {}).get("panel")
    assert api.load_marker_reference("unknown-tissue") is None
