"""Integration checks for the current AutoCycle runtime contract."""

from test_flow import Case, ok


def main():
    # One autonomous cycle now ends at a durable checkpoint with the
    # following opening Review intentionally pending.
    c = Case()
    try:
        r = c.run("1")
        ok(r)

        state = (c.a / "resume-state").read_text()
        assert "STAGE=checkpoint_done" in state, state
        assert c.rows() == []

        print("PASS one-cycle autonomous run preserves review-pending boundary")
    finally:
        c.close()

    # Steering is consumed by a published Plan.
    # Implementation acceptance still waits for the following opening Review.
    c = Case()
    try:
        ident = c.enqueue("CURRENT_CONTRACT_INPUT")

        r = c.run("1")
        ok(r)

        rows = c.rows()
        assert len(rows) == 1, rows
        assert rows[0]["id"] == ident, rows
        assert rows[0]["state"] == "archived", rows

        state = (c.a / "resume-state").read_text()
        assert "STAGE=checkpoint_done" in state, state

        r = c.run(
            "--extend",
            "1",
            REVIEW_STATUS="DONE",
        )
        ok(r)

        rows = c.rows()
        assert len(rows) == 1, rows
        assert rows[0]["state"] == "archived", rows
        assert "STAGE=session_complete" in (c.a / "resume-state").read_text()

        print("PASS input consumed at Plan; implementation acceptance waits for following Review")
    finally:
        c.close()


if __name__ == "__main__":
    main()
