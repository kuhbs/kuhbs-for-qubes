# KUHBS FOR QUBES

The Qubes OS automation system.

THIS PROJECT IS UNDER DEVELOPMENT. PLEASE COME BACK LATER.

## GUI and CLI operation

Each CLI invocation creates `~/.kuhbs/states/cli` and removes it on exit; another
CLI aborts while that file exists. The GUI similarly owns `states/gui`.
Manual CLI commands abort while the GUI is open. Commands launched by the GUI
are marked as GUI children but still use `states/cli`, so GUI operations remain
serial. An uncatchable kill can leave a stale marker for the operator to remove.

The GUI refreshes after startup and after operations or editors that it starts,
but it does not poll for out-of-band changes while it remains open. Restart the
GUI to refresh externally changed cards and button states.

Every action still starts a fresh CLI command that validates current configuration before side effects. Out-of-band changes can therefore leave the display stale, but they do not bypass current validation or operation gates. This is an accepted, managed one-operator risk.

The GUI and CLI use the same target admission plans. Explicit multi-target
commands are strict and run nothing when any selected target is blocked.
`*-all` commands evaluate every configured candidate, print each blocked target
with its reason, and run only the possible targets.

## Qubes feature values

KUHB `features` values are passed to `qvm-features` as written YAML scalars.
Use the exact value required by the Qubes feature consumer. For numeric boolean
features, write `1` or `0`; do not write YAML `True` or `False` and expect KUHBS
to translate it to a different Qubes encoding.

Write configuration mappings explicitly. YAML merge keys (`<<`) are not
supported.

Configuration uses PyYAML scalar rules. Write decimal integers without leading
zeroes; values such as `010` are legacy YAML octal, not decimal ten.

## Launchers

Launcher `command` is one shell command line passed unchanged to `qvm-run`.
Quote it as one YAML string when it contains spaces or shell syntax:

```yaml
command: "/usr/bin/foobar"
command: "test -f /foo && /usr/bin/bar"
```

Use an installed script for multiline or complicated logic, then set `command`
to that script's path.

## Backup and Restore participation

A persistent KUH participates in Backup and Restore only while its current
configuration contains a `backup` block with configured paths. Removing that
block disables both actions for the KUH. An old archive on backup storage does
not enable Restore by itself.

Changing the paths of an enabled KUH does not invalidate its existing archive;
Restore extracts that selected archive without filtering members through the
new path list.

The configured backup VM must already be running before Backup Mount, Backup
Umount, Backup, or Restore. KUHBS never starts it implicitly. The VM normally
uses `prefs.autostart: True`; if it is not running, start it explicitly through
Qubes before using backup storage.
