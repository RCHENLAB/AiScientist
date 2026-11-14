

def test_a_set_input_is_recorded_as_a_non_default_setting():
    """So the technical report says which step read a file of the model's choosing."""
    from aiscientist.agents.research_harness import nondefault_params

    schema = {"type": "object", "properties": {"input": {"type": "string", "default": ""}}}

    recorded = nondefault_params(schema, {"input": "adata_alpha.h5ad"})

    assert [(r["param"], r["value"], r["kind"]) for r in recorded] == [
        ("input", "adata_alpha.h5ad", "selection")]
    assert nondefault_params(schema, {}) == []
