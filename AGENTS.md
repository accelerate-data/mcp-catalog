# Catalog entries

Catalog entries must:

- Use `repoURL` to link to official documentation.
- Include a link to official documentation or setup instructions in the description before the first feature or setup section.
- Lead the description with concrete, product-specific functionality or outcomes.
- Avoid opening with protocol, hosting, or "official server" boilerplate.
- Use `MCP`, not the expanded term "Model Context Protocol".
- Mention hosting, authentication, or official status only when it adds useful context.
- Use `urlTemplate`, not `URLTemplate`, when configuring a URL template.
- Never interpolate credentials, API keys, tokens, passwords, or other sensitive values into `fixedURL` or `urlTemplate`, including query parameters. Use a sensitive top-level `config` field with `usage: header` or OAuth instead. Do not add an entry when its provider requires credentials in the URL.

# Native Obot v0.27 schema

- Declare configuration in top-level `config`, with an explicit `usage` (`env`, `header`, `file`, `dynamicFile`, or `interpolated`) for each field.
- Do not add `serverUserType`, top-level `env`, `remoteConfig.headers`, or `multiUserConfig`.
- Preserve configuration keys, sensitivity, required flags, prefixes, and allowed options. Catalog definitions do not declare `userAllowed`; vMCP component policies determine who supplies values and whether runtimes are shared.
- Keep static non-secret source values in `value`; do not write response-only `static` markers or plaintext secrets.

# Catalog Validation

New catalog entries must use an `entryKey` prefixed with `obot-`.

After changing catalog YAML files, validate all entries before finishing:

1. Check whether a compatible Obot CLI is installed by running `obot mcp validate-catalog-yaml --help`.
2. If that command succeeds, run `obot mcp validate-catalog-yaml --require-entry-key .`.
3. Otherwise, request that the user installs Obot CLI, but do not require it.
4. Run `git diff --check`.
