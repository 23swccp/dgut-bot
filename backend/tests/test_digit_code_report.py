import pytest

from backend.tools.digit_code_report import analyze


def test_leading_zeros_and_repeated_observations_are_preserved():
    report = analyze({"observations": [
        {"timestampMs": 1000, "code": "0078"},
        {"timestampMs": 1500, "code": "0078"},
        {"timestampMs": 6000, "code": "1234"},
    ]})
    assert report["observations"] == 3 and report["displayedRuns"] == 2
    assert report["leadingZeroRuns"] == 1
    assert report["observedChangeIntervalsSeconds"] == {5.0: 1}
    assert report["candidates"] == []


def test_known_recurrence_is_checked_against_holdout_samples():
    values = [1234]
    for _ in range(49):
        values.append((21 * values[-1] + 17) % 10000)
    report = analyze({"observations": [{"timestampMs": 1000000 + i * 5000, "code": f"{value:04d}"}
                                      for i, value in enumerate(values)]})
    recurrence = report["candidates"][0]
    assert any(item["multiplier"] == 21 and item["increment"] == 17 and item["holdoutHits"] == item["holdoutTotal"]
               for item in recurrence["matches"])
    assert recurrence["validatedMatches"] > 0


def test_training_fit_does_not_count_as_validated_when_holdout_breaks():
    values = [1234]
    for _ in range(49):
        values.append((21 * values[-1] + 17) % 10000)
    values[-1] = (values[-1] + 1) % 10000
    report = analyze({"observations": [{"timestampMs": 1000000 + i * 5000, "code": f"{value:04d}"}
                                      for i, value in enumerate(values)]})
    recurrence = report["candidates"][0]
    assert recurrence["trainingMatches"] > 0 and recurrence["validatedMatches"] == 0


@pytest.mark.parametrize("data", [{"observations": []}, {"observations": [{"timestampMs": 1, "code": 78}]},
                                 {"observations": [{"timestampMs": 1, "code": "abcd"}]}])
def test_invalid_samples_are_rejected(data):
    with pytest.raises(ValueError):
        analyze(data)
