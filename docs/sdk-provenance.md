# IM Agent SDK acceptance pin

IMCodex production composition uses the installed public v1 SDK surfaces only.
The acceptance artifact is:

- canonical source commit: `a03f3eab218d0088192f177265b5eebfc87c2e60`
- wheel: `/Users/xbjt/Code/im-agent-sdk/dist/im_agent_sdk-0.1.0a1-py3-none-any.whl`
- SHA-256: `1616b1079248da279068c47c4ea047dab0b4d5aefb129aa3e22017140c2831f4`

The dependency is version-pinned in `pyproject.toml`.  The wheel hash and
source commit are acceptance provenance rather than a second runtime
implementation; `Gateway`, the Codex Application adapter, native Channels,
request/presentation seams, and proactive delivery remain SDK-owned public
authority.
