"""K6: the PC stays awake while recording; the request is cleared afterwards and never blocks a recording."""
from lecturehub.recorder.power import ES_AWAYMODE_REQUIRED, ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake


def test_keep_awake_sets_and_clears():
    calls = []
    with KeepAwake(setter=lambda flags: calls.append(flags) or 1) as awake:
        assert awake.active and awake.away_mode
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED, ES_CONTINUOUS]
    assert not awake.active


def test_keep_awake_without_away_mode_and_failure_never_raise():
    calls = []
    with KeepAwake(setter=lambda flags: (calls.append(flags), 0 if flags & ES_AWAYMODE_REQUIRED else 1)[1]) as awake:
        assert awake.active and not awake.away_mode
    assert calls[-1] == ES_CONTINUOUS

    def broken(flags):
        raise OSError("no power API")

    with KeepAwake(setter=broken) as awake:  # a recording must start anyway (D-050)
        assert not awake.active
