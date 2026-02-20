---
name: Bug report
about: Something is broken
title: '[BUG] '
labels: bug
assignees: ''
---

## Describe the bug

A clear and concise description of what the bug is.

## To reproduce

Steps to reproduce:
1. Run `...`
2. Ask `...`
3. See error

## Expected behaviour

What you expected to happen.

## Actual behaviour

What actually happened. Include the full error message or unexpected output.

## Logs

Enable debug logging and paste the relevant output:

```bash
FASTMCP_LOG_LEVEL=DEBUG make serve-mcp
```

```
[paste logs here]
```

## Environment

- OS: [e.g. macOS 14.5, Ubuntu 22.04]
- Python version: `python --version`
- uv version: `uv --version`
- Model used: [e.g. mem-agent-mlx-4bit, vLLM]
- AI client: [e.g. Claude Desktop 1.x, LM Studio 0.x, ChatGPT]
- Transport: [stdio / http / sse]

## Memory setup

- Memory directory size (approx):
- Number of files in memory:
- Connector used to import (if any):

## Additional context

Any other context about the problem.
