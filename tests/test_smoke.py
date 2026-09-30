"""Placeholder smoke test; real crash tests are added later."""


def test_import() -> None:
    import offline_restore

    assert offline_restore.RestoreManager is not None
