"""The reminder sends only as many template parameters as Meta approved for the template."""


def test_dose_reminder_v2_sends_only_the_dose_label(reminder):
    params = {"1": "evening", "2": "1 tablet with food"}
    assert reminder.template_body_params("dose_reminder_v2", params) == ["evening"]


def test_other_templates_send_every_parameter_in_order(reminder):
    params = {"2": "b", "10": "j", "1": "a"}
    assert reminder.template_body_params("visit_reminder_v1", params) == ["a", "b", "j"]


def test_no_parameters(reminder):
    assert reminder.template_body_params("dose_reminder_v2", {}) == []
