# KUHBS audit false positives

This file records only incorrect audit assumptions and intentional behavior that
is likely to be misreported as a bug. Do not add ordinary requirements,
implemented fixes, test coverage, or general product documentation. Do not
recreate `README-audit.md`; rejected audit findings belong here only.

## Installed KUHB directories are trusted code

Everything inside an installed KUHB directory is trusted local code. The user is responsible for reviewing a KUHB before installing or creating it.

KUHBS validates basic configuration shape, types, enums, and lexical formats. It
does not sandbox or judge trusted hooks, scripts, templates, launchers, or other
repository contents; those files may run privileged dom0/VM actions by design.

Dom0 hooks intentionally receive no implicit target arguments or environment. A hook may update i3 configuration, operate on the current KUH, change another VM such as a NetVM, or perform unrelated trusted orchestration. The hook path and operation phase determine when it runs, not what resource it must manage. Scripts that need names may encode or discover them themselves; do not report missing invoking-KUH context as a bug.

## Supported Qubes OS baseline is trusted

KUHBS trusts the standard Qubes OS dom0 baseline and does not add defensive
existence checks for Qubes-owned files, commands, services, packages, or default
platform components. In particular, `/var/lib/qubes/qubes.xml` exists on the
supported system and is authoritative; hypothetical absence or corruption of
that Qubes-owned file is outside KUHBS's contract.

Do not add findings or code for a supposedly missing Qubes baseline component,
or for hypothetical `qvm-*`, qrexec, or transport failure while performing a
normal inspection. KUHBS trusts Qubes tools and their documented query results;
it does not add tri-state probe wrappers, fallback paths, or malformed-output
recovery. Requested mutation commands still report their ordinary checked exit
status directly.

KUHBS assumes NetVM providers required by managed VMs are already running.
Operations may start a halted provider through ordinary Qubes dependency
behavior and leave it running. Request-wide discovery, snapshotting, and exact
halted/running/paused restoration of external NetVM chains is intentionally not
implemented; that amount of lifecycle code is disproportionate to the unlikely
operator setup. This also applies to the configured Qubes Snitch firewall when
it is acting as an external NetVM. The operator is responsible for provider
power state.

## Qubes Snitch has no supported self-rule cleanup case

Qubes VM names are unique, and Qubes does not allow the configured Snitch VM to
use itself as its NetVM. A second downstream VM therefore cannot legitimately
produce a rule file named for the Snitch VM itself. Qubes Snitch does not create
`<snitch-vm>.yml` for its own local traffic, and the supported KUHBS Snitch
definition does not ship such a starter rule.

A `<snitch-vm>.yml` file can exist only after unsupported manual intervention.
Do not report that removing this manually planted file through the halted
Snitch VM could restart the VM before deletion, and do not add self-rule guards
or an extra shutdown pass for that operator-created state.

## GUI

Batch spinners are intentionally coarse: when one terminal runs multiple
selected KUHBs sequentially, every selected KUHB shows the affected-column
spinner until that terminal closes. This keeps GUI state simple and avoids
pretending to know per-KUHB command progress inside the terminal.

The supported GUI operator has a mouse. KUHB cards and repository rows do not
need full keyboard-only focus, navigation, or activation support; missing
keyboard-only row selection is not a KUHBS bug.

Repository Link preflights the complete selected source set and rejects duplicate
KUHB targets before creating any symlink. Selecting two repository sources with
the same KUHB id therefore fails without a partial Link; do not report partial
mutation from that selection.

Repository Add and Update validate every installed repository KUHB as one set.
KUHB IDs are globally unique across repositories; a duplicate makes Add fail and
clean up its candidate, or makes Update fail before replacing the old checkout.

Selected Restore is intentionally enabled from configured participation, mounted
storage, and lifecycle state rather than treating cached archive status as a
second admission gate. Lifecycle state does not promise that removable backup
media still contains the archive. Restore's request-wide preflight is
authoritative and reports a missing archive without extracting anything.

## Lifecycle state repair

Lifecycle state directories for inactive KUHB IDs and unknown filenames are
intentionally ignored; only known action files for active definitions belong to
startup validation. State corruption caused by an uncatchable hard
kill or manual edits requires manual repair; GUI Remove is not a recovery
mechanism for malformed state contents.

Paused VMs count as active for lifecycle planning, but KUHBS does not restore
pause state. The separate pause daemon owns re-pausing eligible VMs after an
operation.

After guest mutation starts, an operation failure leaves the target in its
current state for debugging, commonly running, and leaves already-stopped
dependents stopped. KUHBS does not roll power state back or persist retry
checkpoints. The operator inspects and fixes the failure, then repairs lifecycle
state or removes and recreates the KUHB as appropriate.

## Dry-run

KUHBS intentionally has no dry-run mode. Command-skipping simulation cannot
faithfully predict Qubes results or local side effects, so tests inject explicit
fake runners instead of exposing execution avoidance as a user feature.

## Upgrade batch failure policy

An Upgrade failure stops all later order groups and the dom0 upgrade after
workers in the current order finish. Orders may encode dependencies, so KUHBS
does not guess that later targets are independent or continue a partial batch.
This applies whether or not the failed definition requests network
confirmation.

## Launchers

Launcher `autostart: True` includes an entry in the user-invoked KUHBS i3/rofi launcher named "Autostart". It does not start the entry during boot or login. Boot-time VM autostart is controlled separately by `prefs.autostart`.

Launcher `command` is one opaque shell command line passed as one `qvm-run`
argument. KUHBS does not parse it as a script; multiline or complicated logic
belongs in an installed script named by `command`.

Qubes OS provides `xfce4-terminal` in dom0, and KUHBS uses that guaranteed dom0
terminal without probing. The configured preferred/fallback terminal resolution
is for target VMs whose installed terminal packages may differ; it is not a
dom0 fallback contract.

## Backup and restore

Requiring backup source paths to be absolute or begin with `~/` is intentional.
Relative paths would depend on KUHBS's current working directory and do not name
a stable backup source.

Rejecting backup configuration on named disposable VMs (`ndp`) is intentional.
Disposable instances do not own persistent state; backup belongs to the
TemplateVM, AppVM, or StandaloneVM that owns durable storage.

Restore participation intentionally follows the current KUH `backup` configuration.
A historical archive does not opt a KUH back in after its `backup` block is
removed; no configured backup paths means no Backup or Restore target. Existing
archives remain restorable when an enabled KUH's path list changes.

KUHBS operations assume their input TemplateVMs have already completed the
required base setup. Early operation prerequisites such as `tar`, `zstd`, and
terminal binaries must therefore exist before per-KUH setup scripts run;
KUHBS does not bootstrap missing operation tools during Restore.

The configured backup VM is an explicit infrastructure prerequisite. Backup
Mount, Backup Umount, Backup, and Restore require it to be running and never
start it through `qvm-run`; GUI controls remain disabled until it is running.
Its normal KUH configuration uses `prefs.autostart: True`.

KUHBS accepts externally mounted backup storage at `/mnt` when `/mnt/kuhbs-backup` exists. The backup-mount helper is optional; users may mount their own storage and still use KUHBS backup/restore logic.

`/mnt` and `/mnt/kuhbs-backup` are code contracts, not defaults. The installed `kuhbs.BackupRead` and `kuhbs.BackupWrite` qrexec services receive no configuration and use that archive root directly. Backup, restore, listing, the GUI, backup-mount, and backup-umount share one mountpoint-plus-directory readiness probe.

The backup qrexec services intentionally run as the backup VM's ordinary default user, not root. `backup-mount` creates `/mnt/kuhbs-backup` as `user:user` mode 0700, and BackupWrite creates archives as that user with mode 0600. Root is needed only for storage setup and mount management. Externally mounted storage must make the archive directory readable and writable by the backup VM's ordinary user; elevating BackupRead or BackupWrite to root is not a fix for incompatible external ownership.

Remove intentionally does not inspect backup storage or block removal of the configured backup VM while `/mnt` or its mapper is active. The supported sequence is `backup-umount` before Remove; force-removing a mounted backup VM is accepted operator error. BackupWrite syncs each successfully completed archive file before reporting success to limit cached-data loss if the operator ignores that sequence, but it does not make killing an active write or mounted filesystem supported.

`backup-umount` closes the configured dm-crypt mapper only when that mapper exists. Externally mounted storage is unmounted without attempting to close a mapper owned outside KUHBS.

`backup-mount` does not run its interactive cryptsetup helper over an existing partial `/mnt` mount or an already-active configured mapper. Those states keep only cleanup available; the operator runs `backup-umount` before retrying `backup-mount`.

KUHBS intentionally does not inspect or validate restore archive members. The backup USB VM, selected media, and archive are trusted; tar and zstd report archive read or extraction failures directly. This also keeps older backups restorable after `backup.paths` or `backup.dom0_paths` changes.

Restore intentionally overlays archive members onto the destination instead of
replacing an exact filesystem snapshot. Matching paths are overwritten, while
destination files absent from the archive remain in place. KUHBS does not clear
source roots or maintain an archive-side deletion manifest.

The shared `states/cli` marker serializes backup-mount, backup-umount, backup,
restore, and every other CLI invocation. KUHBS does not add a separate
backup-storage lock.

An empty `backup.dom0_paths` list intentionally disables dom0 backup. The shared
action planner reports dom0 as blocked during Backup All in that case and reports
any KUHB without configured backup paths for the same reason. Dom0 Restore is
state-gated independently of the current path list and still checks the selected
archive with `test -f` before extraction.

GUI Restore availability intentionally trusts configured archive targets and
the lifecycle state written by successful Backup. It does not duplicate the
Restore command's checked archive-presence test merely to enable or disable a
button. Missing media, a manually removed archive, or incorrectly repaired state
therefore reaches Restore and fails normally; the operator repairs the state or
backup storage rather than KUHBS maintaining a second GUI truth source.

KUHBS does not preserve sparse-file sparseness and does not currently have a good streaming-friendly fix for this. Large sparse files can therefore consume real archive space.

KUHBS backup/restore supports regular files, directories, and intended symlinks. FIFOs, sockets, block devices, character devices, and other special filesystem entries are unsupported.

Backup intentionally preserves normal tar path, content, ownership, and mode
metadata only. POSIX ACLs, extended attributes, SELinux labels, and file
capabilities are outside the backup contract and are not preserved.

`backup.ignore_failed_read` defaults to `False`, so missing or unreadable configured KUH sources fail through tar. A concrete KUH whose live application changes files during backup may set `backup.ignore_failed_read: True` in its own `backup` block. Dom0 independently uses `backup.dom0_ignore_failed_read` from `defaults.yml`.

Backup media uses headerless plain dm-crypt by design for plausible deniability. Users should choose a high-entropy backup password because there is no LUKS2 header/PBKDF hardening.

Fresh-media creation intentionally lets `cryptsetup` prompt directly instead of storing the password in a shell variable. Its verification flow is:

1. Open the raw device with the new password.
2. Create ext4 through that mapping, mount it, and create `/mnt/kuhbs-backup`.
3. Unmount the filesystem and close the mapper.
4. Open the raw device again with a second password entry.
5. Verify that the reopened mapping contains ext4, mount it, and verify `/mnt/kuhbs-backup`.

Plain dm-crypt has no authentication, so seeing the expected filesystem after reopening is what proves that the password was reproduced. If the first entry contained a typo, verification fails; the operator can abort and rerun the already-authorized fresh-format path. Do not report the absence of a pre-format password comparison as a bug.

KUHBS setup cleanup intentionally removes the target VM's `QubesIncoming` tree after successful setup. Finished installed VMs should not retain stale incoming setup artifacts.

The backup-mount retry-loop finding is a false positive against current tested behavior.

Backup-mount may leave a decrypted mapper or mounted filesystem available after mid-script failures to aid debugging. `backup-umount` or manual cleanup is expected afterward.

The backup-mount helper lists likely USB devices but accepts the confirmed block device name the user types. Formatting the wrong device after confirmation is accepted as operator error.

KUHBS backup writes directly to the final archive path instead of writing a second temporary archive and renaming it into place. This is intentional because backup USB space is limited, and keeping both the old archive and the new archive during every write can double the required space. A failed stream can therefore destroy the previous archive and leave a partial final path; the failed lifecycle state and terminal error tell the operator to rerun Backup. Users who want retention should manually rotate `/mnt/kuhbs-backup`, for example by moving it to a dated directory before starting a new backup.

Backup and Restore archive workers keep their simple fail-fast batch boundary. A
VM archive failure prevents the later dom0 stage, and a grouped KUHB job reports
success only when all of its concrete archive targets succeed. Earlier successful
work remains visible in terminal output but is not promoted into a partial-success
summary. KUHBS does not continue or checkpoint the remainder of a failed archive
request.

KUHBS creates per-VM qrexec policy allow files during backup and restore, then removes them once in `finally` blocks with checked `rm -f ...`. The command succeeds whether the policy exists or is already absent. Hard kills and process death remain manual-cleanup cases.

## whonix-gateway-18 in the installer

The dom0 installer sets the label on `whonix-gateway-18` but does not install that template.

This is intentional for the supported Qubes install target: Whonix is installed by default in Qubes, so KUHBS does not need to install the Whonix gateway template itself.

## debian-13-minimal in the installer

The supported install target already has `debian-13-minimal`. The installer
intentionally configures its label but does not install or preflight it; the
commented `qvm-template install` line exists only for development and is not an
enabled dependency or supported installer step.

Commented `qubes-dom0-update` package commands likewise exist only for
development. The supported dom0 baseline already provides the packages used by
the enabled installer and runtime; commented dependency lines are not install
steps and must not be audited as though they execute.

The KUHB definitions under `install/templates/home/user/.kuhbs/my-kuhbs` are
installer test fixtures. The enabled installer intentionally creates an empty
user definition directory and does not copy those fixture definitions into a
real installation.

## Validation false positives

`kuhbs check` and GUI startup intentionally verify each configured base TemplateVM
against one parsed `/var/lib/qubes/qubes.xml` snapshot. Validation does not run a
`qvm-*` command for names or classes already recorded there. KUHBS does not
install base templates; the configured Qubes target must provide them. Create
must clone the base template before any `create-pre` hook or setup script can
run, so no KUHBS hook can repair a missing base template in time. Reporting a
missing or wrong-class configured template is therefore intentional prerequisite
validation, not over-validation.

Reserving KUHB IDs `dom0` and `disp[0-9]*` is intentional. `dom0` is the
Qubes AdminVM and cannot be a managed KUHB ID, while `dispNNN` belongs to Qubes'
automatically generated runtime DisposableVM namespace.

Rejecting duplicate generated Qubes VM names within one KUHB definition or
across the active definition set is intentional. Duplicate instance IDs or
overlapping KUHB definitions would make separate Create operations target the
same Qubes VM name.

YAML merge keys (`<<`) are intentionally unsupported. KUHBS configuration uses
explicit mappings; preserving PyYAML merge behavior is not a parser goal.

PyYAML scalar semantics are accepted. Zero-padded plain integers use legacy
YAML octal interpretation; authors must write decimal integers without leading
zeroes. KUHBS does not replace PyYAML's resolver or add source-text checks for
this standard parser behavior. Likewise, the operator must not use `.nan`,
`.inf`, or `-.inf` as duration values; KUHBS does not add a custom finite-number
type on top of JSON Schema for contrived non-duration scalars.

Lifecycle gates intentionally select the newest action file by filesystem
modification time, and upgrade freshness uses wall-clock timestamps. KUHBS
assumes the dom0 clock does not move backward across operations and does not add
sequence files or future-timestamp recovery logic for clock rollback.

Backup completion means the checked archive pipeline exited successfully and BackupWrite synced that completed archive file. The per-file sync does not flush unrelated filesystems or replace `backup-umount`; killing an active write, removing the mounted backup VM, power loss, and unplugging live media remain operator risks.

`backup.kuh` is allowed to name the future KUHBS backup VM before that VM exists. Startup validation therefore does not require this value to resolve to an existing VM during fresh install.

Qubes feature values are trusted YAML scalars and KUHBS does not infer
feature-specific boolean semantics. YAML booleans stringify as `True` and
`False`; an author whose Qubes feature expects numeric booleans must explicitly
write `1` or `0`. Automatically translating, unsetting, or otherwise
interpreting feature values is outside KUHBS's shallow configuration contract.
This includes YAML `null`: Python stringification passes it to `qvm-features` as
the literal value `None`; KUHBS does not interpret it as an unset request.

Configured filesystem roots use `~` expansion or absolute paths. Relative
`paths.config` and `paths.kuhbs` values are unsupported trusted-operator input;
KUHBS does not reinterpret them or guarantee repository symlink behavior for
them.

KUHB setup payloads use only `<kuhb>/templates/<kind>`. They are copied before setup scripts run and the target VM's complete `/home/user/QubesIncoming` tree is removed after successful setup. Failures inside trusted standalone setup scripts may still occur after the temporary setup template exists so the failed environment can be inspected; `kuhbs remove <sta-kuhb>` cleans up that setup template.

## Qrexec and VM script execution

Rejecting `setup_scripts` on named disposable VMs (`ndp`) is intentional. NDPs
are disposable instances; setup scripts belong to the TemplateVM, AppVM, or
StandaloneVM that owns persistent storage.

Requiring every configured setup-script path to be absolute is intentional.
Relative paths would depend on KUHBS's current working directory and do not have
a stable execution target.

VM-side setup and hook runners may execute copied files from `QubesIncoming` during KUHBS-owned setup flows. KUHBS treats this as trusted local setup state, not as a separate VM-local attacker boundary to harden with root-owned staging.

KUHBS owns the temporary backup/restore policy filenames `30-kuhbs-backup-write-<kuh>.policy` and `30-kuhbs-backup-read-<kuh>.policy`. Overwriting and deleting those exact KUHBS-named files during the operation is intentional.

A trusted setup or hook script basename colliding with `kuhbs-run-script.sh` is accepted as user error. KUHBS does not add extra machinery for that improbable local naming collision.

## Locks and interruption

Every CLI checks `~/.kuhbs/states/cli`, then creates it with `touch` before
configuration validation or Qubes work and removes it with `rm -f` behavior on
exit. The GUI owns `states/gui`; manual CLI invocations abort while it exists,
while GUI-launched children are explicitly marked and still take `states/cli`.
GUI startup also aborts while either marker exists.

These are intentionally plain existence checks, `touch`, and removal rather
than atomic lock acquisition. Exact simultaneous starts remain operator error
under the one-operator contract. An uncatchable kill may leave a stale marker;
the operator removes that file manually after confirming no KUHBS process runs.

KUHBS operation status files are the source of truth for planning. When more than one lifecycle action file exists for a KUHB, the file with the newest modification time is the current state. Upgrade files record health only and do not replace that lifecycle gate. Dom0 uses backup/restore state for admission: no state permits its first backup, and completed backup state permits restore. Commands do not inspect live Qubes state to decide whether an operation is allowed. If the files no longer match reality, manually repair `~/.kuhbs/states/<kuhb>/<action>` before running the next operation.

Lifecycle state transitions intentionally use one direct text write, equivalent
to `echo state > statefile`. KUHBS has one operator and stores only `start`,
`completed`, or `failed`; it does not add temporary-file replacement, signal
deferral, `fsync`, or recovery machinery for the tiny interruption window. An
uncatchable kill at exactly that write is covered by the same manual state-repair
contract. Do not report the lack of an atomic state-file replacement as a bug.

If the user hard-kills KUHBS processes and state files are wrong afterward, that is accepted operator cleanup, not a KUHBS bug. KUHBS cannot reliably inspect whether every trusted hook, setup script, VM command, backup stream, or restore step finished correctly, so it deliberately relies on operation code writing the state files. If those files are wrong, the user must manually debug the current KUHB state and update the state files themselves. Do not report bugs for this hard-kill/manual-repair case.

Explicit multi-target commands are strict: one blocked target prints the shared
plan and aborts before lifecycle state or workers. `*-all` is the only tolerant
expansion; it prints every blocked target and reason, then runs the frozen
possible subset without a partial-plan prompt.

Direct Ctrl+C while KUHBS waits for a visible VM setup or hook terminal stops
the main KUHBS operation and prevents later terminals from being launched. The
already-started dom0 `qvm-run` client and its VM-side terminal/script may remain
until the operator closes or cancels that visible terminal. Preserving that one
operator-visible script chain is intentional; direct cancellation does not own
or forcibly terminate it.

Ctrl+C cancellation snapshots and kills the child processes active at that
instant. A worker could theoretically unregister one finished child while that
snapshot occurs and launch its next command immediately afterward. This requires
interruption in the narrow inter-command scheduling gap, and the executor still
waits for the worker rather than leaving detached work. KUHBS accepts that the
next command may run or the operation may complete in this unlikely case; it
does not add a request-scoped cancellation gate for it.

A local benchmark of 999 direct logged command transitions measured a 35.9 µs
median gap, 91.2 µs at the 99th percentile, and a 1.09 ms maximum. Caller work
and OS scheduling mean there is no strict upper bound, but the ordinary gap is
far too small to justify cancellation machinery under the one-operator model.

## Restore target trust

KUHBS restore assumes the selected archive and target VM/dom0 filesystem are trusted. It does not try to defend against hostile archive contents or pre-existing target filesystem state such as symlink path components.

## Repository import trust

Repo add/update creates one temporary named DisposableVM such as `repo-add-1234`
or `repo-update-1234` from `repos.dispvm_template`. A real installed Bash script
runs through the normal visible KUHBS setup-script terminal and owns clone/fetch,
full commit selection, checkout, local-commit rebase, worktree-change reapply,
and output archive creation. Update prints the final status, history, and diff;
only an exact `y` at its final dom0-apply prompt permits archive creation. The
temporary VM uses the configured `kuhbs <kuhbs-repo@localhost>` identity when a
rebase creates replacement committers; original commit authors remain intact.
The terminal keeps failures available for its normal debug shell before KUHBS
kills and removes the VM.

The configured example SSH key is copied into the fresh VM as a mode-0600 file;
its published public/private pair must never be authorized on a real Git server.
An update checkout enters the VM through `qvm-copy-to-vm`. The approved checkout
archive returns through `qvm-run --pass-io` because Qubes has no normal file-copy
destination for dom0; repository return, dom0 backup, and dom0 restore are the
only accepted pass-io paths. KUHBS trusts the fresh repo VM and git-cloned content
enough to create the tar that dom0 extracts without separate tar-member checks.

Dom0 rejects symlinked repository and KUHB roots. Update installs the candidate
at the repository's real path, validates every returned KUHB plus the complete
active definition set, and restores the old checkout if validation fails.
Existing KUHB symlinks use the stable repository path and therefore follow the
installed candidate or restored old checkout without being recreated.

Repository Update supports ordinary uncommitted file edits and simple local
commits. It preserves tracked, untracked, and ignored file contents, but does not
support or detect advanced Git state such as merge topology, staging boundaries,
extra branches or tags, worktrees, rewritten ancestry, or replacement refs.
Repository checkouts intentionally represent an operator-selected exact commit
and may remain in detached-HEAD state after update. KUHBS preserves supported
commit and worktree content, not attachment to an operator-created branch name.
Git owns repository file modes. Update preserves Git's executable-bit semantics,
but does not preserve arbitrary Unix permission bits on tracked, untracked, or
ignored local customizations; operators must restore special modes through
reviewed setup or hooks.

Repository Remove directly performs recursive deletion after validating the
checkout. Ctrl+C can interrupt that deletion and leave a partial checkout; this
is accepted operator cleanup. KUHBS does not add tombstone renames, deferred
signals, rollback, or recovery for interrupting `rm -rf`-equivalent behavior.

Repository checkout IDs must not be path prefixes of each other. A namespace
directory created for a deeper checkout is not converted into a parent checkout;
operators should use non-conflicting repository URL paths.

## GUI accepted behavior

The GUI paints its complete static interface before startup validation begins.
All tabs and toolbars are visible but disabled while the centered validation
blocker runs. Validated configuration then populates the cards, the blocker
closes, and the interface becomes usable. A broken KUHB YAML enters repair mode:
all operation controls remain disabled, the broken card and its Qubes prerequisite
show red status, and Edit is enabled only for the selected broken KUHB. Closing
Gedit automatically validates again. A global defaults, schema, filesystem, or
Qubes XML failure shows one copyable Close-only error, writes the same text to
stderr, destroys the GUI, and exits nonzero; there is no retry loop. Validation
notices are informational and stay out of stderr.

Manual CLI commands abort while the GUI is open. The GUI refreshes after startup
and after operation terminals or editors that it starts, but it does not
periodically poll for out-of-band configuration, repository, Qubes state, or
backup-storage changes. Restarting the GUI refreshes those external changes.

Every action still starts a fresh CLI command that validates current configuration before side effects. A stale GUI display or button state therefore cannot bypass current validation, lifecycle, planning, or operation gates. Direct manual file or Qubes changes outside KUHBS remain an accepted one-operator risk.

Repository cards use the existence of `my-kuhbs/<id>` as a coarse Linked label;
they do not duplicate exact symlink provenance or same-ID batch validation in the
GUI. Two repository candidates with the same KUHB ID may therefore both appear
Linked or leave Link enabled, but the CLI owns the complete mutation preflight:
`link_kuhbs` rejects duplicate targets before creating any symlink, and Unlink
requires the selected local symlink to resolve to that exact repository source.
Do not report a possible partial Link or require duplicate repository validation
in the GUI.

GUI Edit intentionally opens `gedit` directly because the installed file-browser settings and per-KUHB sidebar root are Gedit-specific.

GUI Edit does not lock a repository checkout or block other GUI actions while
Gedit remains open. It tracks the standalone editor process only to run shared
validation and status refresh after Gedit closes. Under the one-operator
contract, concurrent editor saves and repository replacement are accepted user
error, not a repository transaction requirement.

The installer intentionally replaces Gedit's complete `active-plugins` setting
with `['filebrowser']`. KUHBS owns the dom0 editor setup used by GUI Edit, so
disabling previously enabled Gedit plugins during install or reinstall is
accepted behavior rather than an additive desktop-preference merge.

The installer also intentionally replaces root's and the user's complete XFCE
Terminal configuration and removes legacy `terminalrc` files. KUHBS owns the
dom0 terminal setup used by its visible operation terminals; preserving or
merging unrelated pre-install terminal preferences is not required.


GUI selected batches keep the terminal as the source of operation truth. The GUI tracks the terminal window lifetime for spinners and does not try to stop later shell commands after an earlier selected command fails.

GUI selected-action sensitivity and `*-all` availability consume the same
side-effect-free plans as the CLI using cached lifecycle facts. A GUI `*-all`
click opens the CLI terminal without duplicating its target/reason confirmation;
the CLI rebuilds and prints the current plan before execution.

## VM setup/script execution accepted risks

The VM-side setup/hook exit-status marker under `/tmp` is accepted as part of KUHBS-owned trusted setup flow. KUHBS does not harden this path against a hostile process already running inside the target VM during setup.

If a target VM is already absent, `kuhbs remove` intentionally skips that VM's remove hook. Remove hooks are VM-existence-bound cleanup, not a manifest of all historical dom0 artifacts.

## Launcher accepted behavior

Rejecting duplicate launcher ID/user combinations for the same resolved target
is intentional. Those exact three values generate
`kuhbs-<target>+<user>+<launcher-id>.desktop`; accepting a duplicate would write
the same path twice and silently replace one launcher.

Requiring `template_for_dispvms: True` when a launcher sets `dispvm: True` is
intentional. Qubes cannot use the target AppVM as an unnamed DispVM source
without that preference.

`run_in_terminal: True` generated launchers intentionally use `xfce4-terminal`; KUHBS baseline setup installs it. This path does not use the broader terminal fallback resolver.

Desktop launchers are generated during create and removed during remove. Upgrade
does not regenerate them because package and Flatpak updates must not rewrite dom0
desktop entries; changed launcher YAML is applied through remove and create.

Generated terminal desktop entries use the normal installed defaults. A one-off
CLI `--defaults` path is not persisted into launchers.

Interactive terminal launchers are detached after target and terminal probing.
Failures from the later fire-and-forget `qvm-run` process are not reported back to
the launcher.

## Qubes status parsing

KUHBS accepts the small risk that a transient malformed `qubes.xml` read can make list/GUI status collection fail instead of retrying. Qubes owns that file and normally writes coherent state.

## Upgrade setup scripts are create-only

KUHBS setup scripts are intentionally create-time only. Upgrade uses explicit `hooks/<kind>/update.sh` hooks and package upgrade work; it does not rerun `setup_scripts`.

KUHBS assumes managed VMs are not manually removed, renamed, or otherwise mutated outside KUHBS after a successful create. If a user intentionally breaks that contract, the resulting upgrade/list/lifecycle drift is accepted operator breakage rather than something KUHBS needs to harden around.

## Remove uses the current KUHB definition

KUHBS does not keep a historical manifest for renamed or removed KUHs. Renaming KUHs after create is unsupported; remove uses the current reviewed `kuhb.yml` and old manually-renamed resources are accepted operator cleanup.

## Terminal emulator support

KUHBS intentionally supports only `xfce4-terminal` and `xterm` for configured visible terminals. Terminal title/command syntax is not meant to support arbitrary terminal emulators.

## KUHBS launcher namespace

KUHBS owns generated desktop entries named `kuhbs-*.desktop` under the configured desktop applications directory. User-created files in that namespace are unsupported and may be overwritten or removed by create/remove.

## Desktop applications directory cleanup

KUHBS may remove the configured desktop applications directory when launcher removal leaves it empty. An empty launcher output directory is not preserved just for future launcher generation.
