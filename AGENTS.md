# Catalog entries

Catalog entries must:

- Use `repoURL` to link to official documentation.
- Include a link to official documentation or setup instructions in the description before the first feature or setup section.
- Lead the description with concrete, product-specific functionality or outcomes.
- Avoid opening with protocol, hosting, or "official server" boilerplate.
- Use `MCP`, not the expanded term "Model Context Protocol".
- Mention hosting, authentication, or official status only when it adds useful context.
- Use `urlTemplate`, not `URLTemplate`, when configuring a URL template.
- Point `icon` at a servable raster image: a public HTTPS URL that returns `image/png`, `image/jpeg`, `image/gif` or `image/webp`. Studio's provider-image route rejects SVG and ICO, so such an icon is never delivered and every visit ends in the generic fallback (VD-8178). Prefer the vendor's own `apple-touch-icon.png`; when the vendor serves nothing fetchable, commit a PNG under `icons/` and link its `raw.githubusercontent.com/accelerate-data/mcp-catalog/main/icons/...` URL.
- Never interpolate credentials, API keys, tokens, passwords, or other sensitive values into `fixedURL` or `urlTemplate`, including query parameters. Use a sensitive top-level `config` field with `usage: header` or OAuth instead. Do not add an entry when its provider requires credentials in the URL.

# Native Obot v0.27 schema

- Declare configuration in top-level `config`, with an explicit `usage` (`env`, `header`, `file`, `dynamicFile`, or `interpolated`) for each field.
- Do not add `serverUserType`, top-level `env`, `remoteConfig.headers`, or `multiUserConfig`.
- Preserve configuration keys, sensitivity, required flags, prefixes, and allowed options. Catalog definitions do not declare `userAllowed`; vMCP component policies determine who supplies values and whether runtimes are shared.
- Keep static non-secret source values in `value`; do not write response-only `static` markers or plaintext secrets.

# Containerized entry contract

A containerized entry's `containerizedConfig.args` and `config` describe what Obot
passes at runtime, not what the security scan can reproduce.

- Obot starts the image with the entry's `args` and, for each declared input the
  deployment has filled in, an environment variable. It also sets
  `MMMCP_META_ENV` to the comma-separated names of those variables
  (`pkg/mcp/docker.go` in `accelerate-data/obot`).
- Obot's `mcp-images` images run through an `mmmcp.sh` wrapper that forwards an
  environment variable to its child process only when the name appears in
  `MMMCP_META_ENV`. An image whose server expects a CLI flag therefore works when
  the deployment supplies the variable, even though the entry declares no flag.
- The mcpwn scan cannot do the same: it runs on an unprivileged pull request
  token with no repository secrets, so it supplies only an entry's static
  `config` values. An entry that cannot start without an operator-supplied input
  is declared `scan: manual` in `.github/mcpwn-scan-coverage.yaml` with the
  failure it exits with. Do not "fix" such an entry by changing its `args` — the
  declared contract is what Obot uses.

# Catalog Validation

New catalog entries must use an `entryKey` prefixed with `obot-`.

`scripts/curated-entry-contract/validate-curated-entries.py` also rejects an `icon` whose path ends in `.svg` or `.ico`; the Curated Entry Contract workflow runs it on every pull request.

After changing catalog YAML files, validate all entries before finishing:

1. Check whether a compatible Obot CLI is installed by running `obot mcp validate-catalog-yaml --help`.
2. If that command succeeds, run `obot mcp validate-catalog-yaml --require-entry-key .`.
3. Otherwise, request that the user installs Obot CLI, but do not require it.
4. Run `git diff --check`.
