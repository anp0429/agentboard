"""Keep collection out of tests/fixtures: the trees under it are gate
TARGETS (tiny repos the e2e tests copy into sandboxes), not part of this
suite. Collecting them directly would run a fixture's baseline tests as if
they were agentboard's own -- and would break the day a fixture ships a
deliberately red test."""

collect_ignore = ["fixtures"]
