set shell := ["bash", "-cu"]

repo := justfile_directory()
sources := "github jira bitbucket inbox schedule watchdog stalled"

# List recipes
default:
    @just --list

# Install and start launchd daemons (all, or one: `just install github`)
install source="all":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "$HOME/Library/Logs/watchtower" "$HOME/.local/state/watchtower" \
             "$HOME/.config/watchtower" "$HOME/Library/LaunchAgents"
    chmod 700 "$HOME/.config/watchtower"
    if [ ! -f "$HOME/.config/watchtower/config.json" ]; then
        cp "{{repo}}/config.example.json" "$HOME/.config/watchtower/config.json"
        chmod 600 "$HOME/.config/watchtower/config.json"
        echo ">>> seeded ~/.config/watchtower/config.json — EDIT IT (profiles, suppress ids, Jira token)"
    fi
    case "$PATH" in *"|"*) echo "PATH contains '|' — cannot bake it into plists; clean it and re-run"; exit 1;; esac
    for s in {{sources}}; do
        if [ "{{source}}" != "all" ] && [ "{{source}}" != "$s" ]; then continue; fi
        plist="$HOME/Library/LaunchAgents/com.watchtower.$s.plist"
        sed -e "s|__SOURCE__|$s|g" -e "s|__REPO__|{{repo}}|g" -e "s|__HOME__|$HOME|g" \
            -e "s|__PATH__|$PATH|g" \
            "{{repo}}/launchd/com.watchtower.SOURCE.plist.template" > "$plist"
        launchctl unload "$plist" 2>/dev/null || true
        launchctl load -w "$plist"
        echo "loaded com.watchtower.$s"
    done
    echo "next: edit ~/.config/watchtower/config.json if needed, then 'just status'"

# Unit tests for the daemons, run UNDER the live-write audit: fails if any test touches ~/.local/state/watchtower or ~/Library/Logs/watchtower
test:
    python3 -W error::ResourceWarning "{{repo}}/tests/live_write_audit.py" -v

# The same run, quiet — the audit verdict and the totals only
test-audit:
    python3 "{{repo}}/tests/live_write_audit.py"

# Daemon liveness, stored watermark timestamps, and the last event per profile log
status:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "--- launchd ---"
    launchctl list | grep com.watchtower || echo "(none loaded)"
    echo "--- watermarks ---"
    for f in "$HOME"/.local/state/watchtower/*/*.json; do
        [ -e "$f" ] || continue
        since=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('since','?'))" "$f" 2>/dev/null || echo '?')
        echo "$(basename "$(dirname "$f")")/$(basename "$f" .json): since=$since"
    done
    echo "--- last event per log ---"
    for f in "$HOME"/Library/Logs/watchtower/*-*.log; do
        [ -e "$f" ] || continue
        case "$f" in *daemon-*) continue;; esac
        echo "$(basename "$f"): $(tail -1 "$f" 2>/dev/null || echo '(empty)')"
    done

# Is the fleet current? Loaded source hash vs the file on disk, per profile and source
fleet:
    python3 "{{repo}}/daemons/watchdog_wake.py" --fleet

# Stop and remove daemons (all, or one: `just uninstall jira`)
uninstall source="all":
    #!/usr/bin/env bash
    set -euo pipefail
    for s in {{sources}}; do
        if [ "{{source}}" != "all" ] && [ "{{source}}" != "$s" ]; then continue; fi
        plist="$HOME/Library/LaunchAgents/com.watchtower.$s.plist"
        launchctl unload -w "$plist" 2>/dev/null || true
        rm -f "$plist"
        echo "removed com.watchtower.$s"
    done

# Follow one profile's log: `just logs acme github`
logs profile source="github":
    tail -n 20 -F "$HOME/Library/Logs/watchtower/{{profile}}-{{source}}.log"

# Record that this session HANDLED wakes: `just acted acme [inbox] [13]`. With no number it clears to the tail — refused while an open event was never shown to you by `just unactioned`
acted profile source="" through="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--profile "{{profile}}")
    [ -n "{{source}}" ] && args+=(--source "{{source}}")
    [ -n "{{through}}" ] && args+=(--through "{{through}}")
    python3 "{{repo}}/hooks/wt_acted.py" "${args[@]}"

# Clear to the tail INCLUDING open events `just unactioned` never showed you — deliberate use only: `just acted-including-unseen acme [inbox]`
acted-including-unseen profile source="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--profile "{{profile}}" --all-including-unseen)
    [ -n "{{source}}" ] && args+=(--source "{{source}}")
    python3 "{{repo}}/hooks/wt_acted.py" "${args[@]}"

# The current stalled set for a profile, on demand — report-only, touches nothing: `just stalled acme`
stalled profile:
    @python3 "{{repo}}/daemons/stalled_wake.py" --report "{{profile}}"

# From a session: take ownership of a profile's drain cursor from a dead or abandoned session; from a terminal: release it. Clears nothing: `just takeover acme`
takeover profile:
    @python3 "{{repo}}/hooks/wt_acted.py" --profile "{{profile}}" --take-over

# Wakes delivered to a session and never acted on — and the record that you saw them, which a blanket `just acted` needs: `just unactioned acme`
unactioned profile:
    @python3 "{{repo}}/hooks/wt_acted.py" --profile "{{profile}}" --list

# Print the Stop-hook settings.json entry for a profile: `just hook acme`
hook profile:
    #!/usr/bin/env bash
    set -euo pipefail
    echo 'Add to ~/.claude/settings.json (merge into any existing "hooks" object):'
    echo ''
    python3 - "{{repo}}" "{{profile}}" <<'EOF'
    import json, sys
    repo, profile = sys.argv[1], sys.argv[2]
    entry = {"hooks": {"Stop": [{"hooks": [{
        "type": "command",
        "command": "python3 %s/hooks/stop_drain.py --profile %s" % (repo, profile),
        "timeout": 10}]}]}}
    print(json.dumps(entry, indent=2))
    EOF
    echo ''
    echo 'The script lives in this repo (version-controlled, reviewable) rather than'
    echo 'under ~/.claude/hooks — that path is a symlink into iCloud Drive, which'
    echo 'agent sandboxes refuse to read (EPERM).'
    echo ''
    echo 'Registered in the global ~/.claude/settings.json, every session on this host runs'
    echo 'it. To serve only sessions in the seat project, set the session roots too (this'
    echo 'host only; opt-in): just session-roots {{profile}} <absolute project root> ...'

# Opt-in, this host only: the project roots whose sessions a profile's Stop hook serves; any other session's hook does nothing. `just session-roots acme ~/Development/acme` sets (absolute paths only), no roots shows, `--clear` removes
[no-cd]
[positional-arguments]
session-roots profile *roots:
    @python3 "{{repo}}/hooks/wt_roots.py" --profile "$@"

# Print the Monitor commands a session should arm for a profile
arm profile:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Arm one persistent Monitor per source this profile uses:"
    for s in {{sources}}; do
        f="$HOME/Library/Logs/watchtower/{{profile}}-$s.log"
        echo "  tail -n 0 -F $f | grep -E --line-buffered '^type=|^error:'"
    done
    echo "(only arm sources the profile actually configures; check ps for an existing tail first)"
