"""Small-hours rows filed under a day header are the next day (US night games)."""
from freesky.free_sky_hybrid import StepDaddyHybrid


def test_wrap_after_evening():
    ev = [{"time": t} for t in ["16:00", "23:45", "00:08", "02:30", "03:00"]]
    StepDaddyHybrid._mark_day_offsets(ev)
    assert [e["day_offset"] for e in ev] == [0, 0, 1, 1, 1]


def test_morning_rows_stay_same_day():
    ev = [{"time": t} for t in ["00:00", "01:00", "08:30", "18:00"]]
    StepDaddyHybrid._mark_day_offsets(ev)
    assert [e["day_offset"] for e in ev] == [0, 0, 0, 0]


def test_boxing_shape():
    ev = [{"time": t} for t in ["18:00", "18:00", "00:00", "00:00", "01:00"]]
    StepDaddyHybrid._mark_day_offsets(ev)
    assert [e["day_offset"] for e in ev] == [0, 0, 1, 1, 1]


if __name__ == "__main__":
    test_wrap_after_evening(); test_morning_rows_stay_same_day(); test_boxing_shape()
    print("schedule day offset ok")
