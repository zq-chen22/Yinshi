# Yinshi bridge development and deployment rules

These rules concern the codex-feishu-bridge component.

- Canonical source is this Yinshi repository. Make public feature/fix changes in
  its development worktree, test them, and push them here. Do not maintain
  independent unpublished patches in legacy installations or site-packages.
- Read the relevant component vibe_log before work. Before the final reply,
  append a Beijing-time entry with user request, answer summary, code changes,
  commands, verification, artifacts, risks and next steps. Record verifiable
  facts only; corrections are separate entries. Public logs must be sanitized.
- Production uses immutable release tags and a full Git SHA from deploy/stable.json,
  not an untested git pull. Never rewrite a published tag. Run the component
  tests, build the wheel, and verify it against committed source before promotion.
- Use scripts/build-fleet-artifact.py and scripts/fleet-controller.py for fleet
  releases. Keep the private node inventory outside Git. Confirm the installed
  package, actual running process path, CLI version, model catalog and receipts.
- Upgrade only at an idle/drained boundary. Do not forcibly stop live turns for
  a routine release, and do not kill research workers that outlive their turns.
  Verify generated systemd units with the real parser before stopping a service.
- Preserve dirty legacy worktrees, host identities, owner pairing, group bindings,
  history, working-directory authorization, proxy settings and staggered timers.
  Never copy another host's SQLite database, login data or App credentials.
- Roll back configuration and service paths on startup failure, but never replace
  the live database with an older snapshot that would lose newly received messages.
- Read actual CLI model/effort/Fast capabilities. A model policy revision applies
  once; subsequent code releases must not overwrite later user-specific choices.
- Keep delivery durable and idempotent. Claim success only after verifying it;
  distinguish staged/deferred nodes from nodes that actually loaded the release.
- Secrets never go to Git, public logs, attachments or command output. SSH trust
  must remain strict; do not bypass host-key checks to make deployment appear done.
