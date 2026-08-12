# IM Agent SDK acceptance pin

IMCodex production composition uses the installed public v1 SDK surfaces only.
The acceptance artifact is:

- canonical source commit: `a72b24a2558c8b2aa48b05214588da4fc1a434db`
- wheel: `/Users/xbjt/Code/im-agent-sdk/dist/im_agent_sdk-0.1.0a1-py3-none-any.whl`
- SHA-256: `e160c31dd5661c9b419193c9ad3ddb1deb676949fc4a730ed36c65710885572d`

The dependency is version-pinned in `pyproject.toml`.  The wheel hash and
source commit are acceptance provenance rather than a second runtime
implementation; `Gateway`, the Codex Application adapter, native Channels,
request/presentation seams, and proactive delivery remain SDK-owned public
authority.
