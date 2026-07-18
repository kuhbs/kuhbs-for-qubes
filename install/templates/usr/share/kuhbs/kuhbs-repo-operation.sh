# Purpose: Add or update one trusted Git checkout entirely inside a temporary repository DispVM
# Scope: The classic KUHBS terminal runner owns visible output, failure debugging, and exit status

mode="$1"
clone_url="$2"
branch="$3"
commit_count="$4"
input_archive="$5"
output_archive="$6"
ssh_key="$7"
work_dir="$8"
shift 8
linked_kuhbs=("$@")
repo="$work_dir/repo"
revision="refs/remotes/origin/$branch"
stashed=False
old_base=""
old_head=""
local_commits=()

# Start from one empty work directory because the named DispVM owns only this repository operation
rm -rf "$work_dir"
rm -f "$output_archive"
mkdir -p "$repo"

# The example key comes from validated defaults and is copied as a file so its contents never enter command logs
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
mv "$ssh_key" "$HOME/.ssh/id_ed25519"
chmod 600 "$HOME/.ssh/id_ed25519"

if [[ "$mode" == "add" ]]; then
    # Clone only the configured branch before offering its recent full commit IDs
    rmdir "$repo"
    git clone --branch "$branch" --single-branch -- "$clone_url" "$repo"
else
    # Restore the complete dirty checkout so all Git and local-change work stays outside dom0
    tar -C "$repo" -xzf "$input_archive"
    rm -f "$input_archive"
    old_head="$(git -C "$repo" rev-parse HEAD)"
    old_base="$(git -C "$repo" merge-base "$old_head" "$revision")"
    mapfile -t local_commits < <(git -C "$repo" rev-list --reverse "$old_base..$old_head")

    # Git status does not report commits, so stash only separate tracked, untracked, or ignored worktree changes
    if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=normal --ignored)" ]]; then
        git -C "$repo" stash push --all --message "KUHBS local edits before update"
        stashed=True
    fi
    git -C "$repo" fetch origin
fi

# Read the configured number of commits from the fetched branch and keep their full IDs for audited selection
mapfile -t commits < <(git -C "$repo" log -n "$commit_count" --format=%H "$revision")
if [[ "${#commits[@]}" == "0" ]]; then
    echo "Branch has no commits: $branch" >&2
    exit 1
fi

echo
echo "Available commits on $branch:"
echo
for number in "${!commits[@]}"; do
    commit="${commits[$number]}"
    timestamp="$(git -C "$repo" show -s --format=%cI "$commit")"
    subject="$(git -C "$repo" show -s --format=%s "$commit")"
    # sed's list form escapes terminal controls supplied by an untrusted Git endpoint before display
    safe_subject="$(echo -n "$subject" | LC_ALL=C sed -n 'l' | sed 's/\$$//')"
    echo "$((number + 1)) $commit"
    echo "  $timestamp: $safe_subject"
    echo
done

# A short number is easy to type after the user audits the full commit ID outside Qubes
while true; do
    echo -n "Select the commit that was audited [1-${#commits[@]}]: "
    read -r answer
    if [[ "$answer" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= ${#commits[@]} )); then
        selected="${commits[$((answer - 1))]}"
        break
    fi
    echo "Enter a number from 1 to ${#commits[@]}, or press CTRL+C to cancel"
done

if [[ "$mode" == "update" ]]; then
    changed_linked=()
    # Warn only when the chosen upstream range changes a KUHB currently linked into the active local set
    for kuhb_id in "${linked_kuhbs[@]}"; do
        if ! git -C "$repo" diff --quiet "$old_base" "$selected" -- "$kuhb_id"; then
            changed_linked+=("$kuhb_id")
        fi
    done
    if (( ${#changed_linked[@]} > 0 )); then
        echo
        echo "This update changes linked KUHBs:"
        echo
        for kuhb_id in "${changed_linked[@]}"; do
            echo "  $kuhb_id"
        done
        echo
        echo "Existing Qubes are not automatically recreated"
    fi
fi

if [[ "$mode" == "update" && "${#local_commits[@]}" -gt 0 ]]; then
    # Rebase user-created commits onto the audited upstream commit while leaving conflicts open for debugging
    if ! git -C "$repo" rebase --onto "$selected" "$old_base" "$old_head"; then
        echo
        echo "KUHBS could not rebase local commits onto the selected commit" >&2
        echo "Resolve or inspect the conflict in this temporary repository VM" >&2
        exit 1
    fi
else
    git -C "$repo" checkout --detach "$selected"
fi

if [[ "$stashed" == "True" ]]; then
    # Apply every pre-update worktree change after checkout/rebase and keep a conflict visible in the debug shell
    if ! git -C "$repo" stash apply "stash@{0}"; then
        echo
        echo "KUHBS could not reapply local worktree changes" >&2
        git -C "$repo" diff --name-only --diff-filter=U | while read -r conflict; do
            echo "  $conflict" >&2
        done
        exit 1
    fi
    git -C "$repo" stash drop "stash@{0}"
fi

if [[ "$mode" == "update" ]]; then
    # Review the fully rebased checkout and reapplied local edits before anything returns to dom0
    echo
    echo "Repository Update Review"
    echo
    echo "Selected upstream commit: $selected"
    echo "Final HEAD: $(git -C "$repo" rev-parse HEAD)"
    echo
    echo "Status:"
    git -C "$repo" status --short --branch --ignored
    echo
    echo "Recent history:"
    git -C "$repo" log --graph --decorate --oneline -n "$commit_count"
    echo
    echo "Diff stat:"
    git -C "$repo" diff --stat "$selected"
    echo
    echo "Tracked diff:"
    git -C "$repo" diff "$selected"
    echo
    echo -n "Apply this Repository Update to dom0? [y/N] "
    read -r apply || apply=""
    if [[ "$apply" != "y" ]]; then
        echo "Update cancelled; current dom0 repo is unchanged"
        exit 1
    fi
fi

# Return the full checkout including .git and trusted local files in one archive for dom0 installation
tar -C "$repo" -czf "$output_archive" .
