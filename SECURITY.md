# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's
[private vulnerability reporting](https://github.com/ianderso/gramps-evidence-mcp/security/advisories/new)
(the **Report a vulnerability** button on the repository's Security tab), not
in a public issue. Include what an attacker controls, what they gain, and the
steps to reproduce it.

You should hear back within a week. Fixes are released for the latest version
only.

## Scope

In scope: this server — how it handles the Gramps Web credentials, what it
reads from and writes to local disk, the requests it makes, what it discloses
about living people, and anything a tool argument or an API response can make
it do.

Out of scope: Gramps Web and `gramps-webapi`, which this project does not
maintain. Report problems with those to the
[Gramps project](https://github.com/gramps-project/gramps-web-api).

## The security model, briefly

- **The server has write access to a tree of living people.** That is its
  purpose, and it is why every tool declares MCP annotations: a client can
  approve the 46 read-only tools automatically and ask before anything that
  adds, changes or deletes. Use a dedicated Gramps Web user; a user with a
  read-only role turns every write tool into a permission error.
- **Credentials stay in the environment.** The user name and password are sent
  only to `GRAMPS_MCP_API_URL`, to obtain a token, and the token only there.
  None of them is logged or included in a tool result. The TOML file never
  holds a credential, and a `.env` file is read from the working directory
  only.
- **Bulk output is privacy-filtered by default.** Private records and
  probably-living people are left out of searches, queries, tree walks,
  timelines, reports and the reference layer. The README's Privacy section
  lists every tool, what it leaves out, and what is not filtered — a lookup by
  id, the full backup, and events judged by their own flag only. A caller can
  lift the filter for one call with `include_private`; that is logged by tool
  name, so an operator can see when it happened, and it lasts only that call.
- **Local disk: four reads, three writes.** The server reads a `.env` in its
  working directory, the TOML file, and the reference GEDCOMs it lists, and it
  reads a file the model names when uploading media — only an image, PDF, audio
  or video file, so a key or a `.env` cannot be copied into the tree. It
  writes parsed-GEDCOM caches and default backups under
  `~/.cache/gramps-evidence-mcp/`, and `export_backup` writes a full-tree
  export to a path the model chooses, using exclusive creation, so it cannot
  overwrite or truncate an existing file.
- **Logs hold ids, handles and operation names**, never record contents. The
  HTTP library's per-request log is silenced because a query filter travels in
  the URL, and an unexpected error is logged as its stack without its message.
- **Tool results carry untrusted text.** Notes, source text, OCR output and
  the facts in a reference GEDCOM were written by people other than the
  operator, sometimes long ago and sometimes by strangers, and reach the model
  verbatim, which makes them a channel for prompt injection. An injection that
  leads the model to misuse this server's own tools is in scope; one that
  leads it to misuse other tools the client has connected is a client concern.
- **Every id is escaped whole before it reaches a request path** — slashes
  included — and `.` and `..` are refused, so a tool argument cannot redirect
  a request to another API route.
- **The HTTP transport has no authentication of its own.** It listens on
  `127.0.0.1` unless told otherwise, and there the MCP SDK refuses requests
  whose `Host` is not local. Anything reachable beyond the machine belongs
  behind an authenticating proxy, as the README's Remote access section
  describes.
