"""Unit test for the "ignore teacher-role courses" fix: list_courses must
restrict to courses where the user is enrolled as a student."""

from unittest.mock import MagicMock

from agent.graph.nodes import classroom


def test_list_courses_restricts_to_student_role():
    service = MagicMock()
    service.courses.return_value.list.return_value.execute.return_value = {
        "courses": [{"id": "c1", "name": "SCD"}]
    }

    result = classroom.list_courses(service)

    assert result == [{"id": "c1", "name": "SCD"}]
    _, kwargs = service.courses.return_value.list.call_args
    assert kwargs.get("studentId") == "me"
    assert kwargs.get("courseStates") == ["ACTIVE"]


def test_list_courses_paginates_with_student_filter_on_every_page():
    service = MagicMock()
    service.courses.return_value.list.return_value.execute.side_effect = [
        {"courses": [{"id": "c1", "name": "SCD"}], "nextPageToken": "p2"},
        {"courses": [{"id": "c2", "name": "DAA"}]},
    ]

    result = classroom.list_courses(service)

    assert result == [{"id": "c1", "name": "SCD"}, {"id": "c2", "name": "DAA"}]
    for call in service.courses.return_value.list.call_args_list:
        assert call.kwargs.get("studentId") == "me"
