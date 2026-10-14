"""Profiles are JSON Merge Patches (RFC 7396) over the project file."""

from fab0_engine.merge import merge_patch


def test_merge_patch_follows_rfc_7396():
    target = {"a": 1, "b": {"c": 2, "d": 3}}
    assert merge_patch(target, {"b": {"c": None, "e": 4}, "f": [1]}) == {"a": 1, "b": {"d": 3, "e": 4}, "f": [1]}
    assert target == {"a": 1, "b": {"c": 2, "d": 3}}, "the target is not changed"
    assert merge_patch(target, [1]) == [1]
    assert merge_patch(None, {"x": None, "y": 1}) == {"y": 1}
