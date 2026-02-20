## Summary

What does this PR do? (1–3 sentences)

## Type of change

- [ ] Bug fix
- [ ] New feature (new connector, integration, or agent capability)
- [ ] Refactor / cleanup
- [ ] Documentation
- [ ] Tests only

## Changes

-
-
-

## Testing

How did you test this?

- [ ] Added / updated unit tests in `tests/`
- [ ] Tested manually with Claude Desktop
- [ ] Tested manually with LM Studio
- [ ] Tested manually with ChatGPT
- [ ] Tested the CLI (`make chat-cli`)

## For new connectors

- [ ] Inherits from `BaseMemoryConnector`
- [ ] Implements `extract_data()`, `organize_data()`, `generate_memory_files()`
- [ ] Registered in `memory_connect.py`
- [ ] README updated with usage examples
- [ ] At least one test in `tests/`

## Checklist

- [ ] `uv run pytest tests/ -v` passes
- [ ] `uv run black .` applied
- [ ] No secrets or personal data committed
