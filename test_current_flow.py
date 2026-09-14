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

    # Human input stays active after the implementation checkpoint.
    # The following opening Review is what may verify COMPLETE and archive it.
    c = Case()
    try:
        ident = c.enqueue("CURRENT_CONTRACT_INPUT")

        r = c.run("1")
        ok(r)

        rows = c.rows()
        assert len(rows) == 1, rows
        assert rows[0]["id"] == ident, rows
        assert rows[0]["state"] == "active", rows

        state = (c.a / "resume-state").read_text()
        assert "STAGE=checkpoint_done" in state, state

        r = c.run(
            "2",
            "--extend-budget",
            REVIEW_STATUS="DONE",
            INPUT_STATUS_OVERRIDE="COMPLETE",
        )
        ok(r)

        rows = c.rows()
        assert len(rows) == 1, rows
        assert rows[0]["state"] == "archived", rows
        assert not (c.a / "resume-state").exists()

        print("PASS instructed work archives only after following verified Review")
    finally:
        c.close()


if __name__ == "__main__":
    main()
